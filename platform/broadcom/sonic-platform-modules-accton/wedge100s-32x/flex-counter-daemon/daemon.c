/* daemon.c — Flex counter daemon for Wedge100S-32X.
 *
 * Replaces FlexCounter for breakout sub-ports (<4 lanes) where SAI
 * get_port_stats fails on Tomahawk.  Polls bcmcmd 'show c all' every
 * POLL_INTERVAL seconds and:
 *   1. Removes breakout ports from FLEX_COUNTER_TABLE (DB 5)
 *   2. Writes SAI stat fields to COUNTERS:<oid> in COUNTERS_DB (DB 2)
 *   3. Computes EWMA-smoothed rates matching port_rates.lua behavior
 *   4. Detects syncd restarts via OID changes and re-interlocks
 *
 * Runs on the host (not in syncd container).
 */
#include "bcmcmd_client.h"
#include "stat_map.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <syslog.h>
#include <time.h>
#include <glob.h>
#include <hiredis/hiredis.h>

#define DAEMON_NAME        "flex-counter-daemon"
#define POLL_INTERVAL_S    3
#define CONNECT_TIMEOUT_MS 50
#define MAX_LANE_MAP       512
#define MAX_PS_MAP         256
#define MAX_FLEX_PORTS     64
#define REDIS_TIMEOUT_MS   500

/* Above this many breakout ports, use bulk 'show c all' instead of
 * per-port queries.  Per-port is ~17ms/port; bulk is ~2s flat.
 * Crossover is ~120 ports, but we use 64 as a conservative threshold
 * to account for socket overhead variance on a loaded system. */
#define PER_PORT_THRESHOLD 64

/* EWMA smoothing alpha — matches port_rates.lua default. */
#define RATES_ALPHA        0.18

/* stat_map array indices for rate-relevant counters. */
#define IDX_IN_OCTETS         0
#define IDX_IN_UCAST_PKTS     1
#define IDX_IN_NON_UCAST_PKTS 2
#define IDX_OUT_OCTETS         8
#define IDX_OUT_UCAST_PKTS     9
#define IDX_OUT_NON_UCAST_PKTS 10

/* ---- per-breakout-port state ---- */

typedef struct {
    char     eth_name[32];
    char     oid[80];
    char     bcm_name[BCMCMD_PORT_NAME_LEN];
    int      n_lanes;
    int      flex_removed;     /* already deleted from DB 5 */
    int      rate_state;       /* 0=none, 1=have_last, 2=rates_active */
    int      zero_cycles;      /* cycles BCM returned 0 while awaiting baseline */
    struct timespec last_time;
    uint64_t last_in_oct, last_out_oct;
    uint64_t last_in_ucast, last_in_non_ucast;
    uint64_t last_out_ucast, last_out_non_ucast;
    double   smooth_rx_bps, smooth_tx_bps;
    double   smooth_rx_pps, smooth_tx_pps;
} flex_port_t;

static flex_port_t g_flex[MAX_FLEX_PORTS];
static int         g_n_flex = 0;

/* ---- lane map (BCM config: lane -> SDK port) ---- */

typedef struct {
    uint32_t physical_lane;
    int      sdk_port;
} lane_entry_t;

static lane_entry_t g_lane_map[MAX_LANE_MAP];
static int          g_lane_map_size = 0;

/* ---- ps map (bcmcmd ps: SDK port -> port name) ---- */

static struct {
    int  sdk_port;
    char name[BCMCMD_PORT_NAME_LEN];
} g_ps_map[MAX_PS_MAP];
static int g_ps_map_size = 0;

/* ---- counter cache ---- */

static counter_cache_t g_cache;

/* ---- signal handling ---- */

static volatile sig_atomic_t g_running = 1;

static void sig_handler(int sig)
{
    (void)sig;
    g_running = 0;
}

/* ---- socket path ---- */

static const char *g_socket_path = BCMCMD_SOCKET_HOST;

/* ---- BCM config parser ---- */

static void parse_bcm_config(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        syslog(LOG_ERR, "%s: cannot open BCM config '%s': %m", DAEMON_NAME, path);
        return;
    }
    char line[256];
    while (fgets(line, sizeof(line), f) && g_lane_map_size < MAX_LANE_MAP) {
        int sdk_port, phys_lane, speed;
        if (sscanf(line, "portmap_%d.0=%d:%d", &sdk_port, &phys_lane, &speed) == 3) {
            g_lane_map[g_lane_map_size].physical_lane = (uint32_t)phys_lane;
            g_lane_map[g_lane_map_size].sdk_port      = sdk_port;
            g_lane_map_size++;
        }
    }
    fclose(f);
    syslog(LOG_INFO, "%s: parsed %d lane entries from %s",
           DAEMON_NAME, g_lane_map_size, path);
}

static const char *auto_detect_bcm_config(void)
{
    static char path[256];
    /* Container path first, then host path. */
    const char *patterns[] = {
        "/usr/share/sonic/hwsku/*.config.bcm",
        "/usr/share/sonic/device/*/Accton-WEDGE100S*/*.config.bcm",
    };
    for (int i = 0; i < 2; i++) {
        glob_t gl;
        if (glob(patterns[i], 0, NULL, &gl) == 0 && gl.gl_pathc > 0) {
            strncpy(path, gl.gl_pathv[0], sizeof(path) - 1);
            path[sizeof(path) - 1] = '\0';
            globfree(&gl);
            return path;
        }
        globfree(&gl);
    }
    return NULL;
}

/* ---- lookup helpers ---- */

static int sdk_port_for_lane(uint32_t lane)
{
    for (int i = 0; i < g_lane_map_size; i++)
        if (g_lane_map[i].physical_lane == lane)
            return g_lane_map[i].sdk_port;
    return -1;
}

static const char *port_name_for_sdk(int sdk_port)
{
    for (int i = 0; i < g_ps_map_size; i++)
        if (g_ps_map[i].sdk_port == sdk_port)
            return g_ps_map[i].name;
    return NULL;
}

static int count_lanes(const char *s)
{
    int n = 1;
    for (; *s; s++)
        if (*s == ',') n++;
    return n;
}

/* ---- bcmcmd ps map refresh ---- */

static int refresh_ps_map(void)
{
    int fd = bcmcmd_connect(g_socket_path, CONNECT_TIMEOUT_MS);
    if (fd < 0) return -1;

    int sdk_ports[BCMCMD_MAX_PORTS];
    char names[BCMCMD_MAX_PORTS][BCMCMD_PORT_NAME_LEN];
    int n = bcmcmd_ps(fd, sdk_ports, names, BCMCMD_MAX_PORTS);
    bcmcmd_close(fd);

    if (n < 0) return -1;

    g_ps_map_size = n;
    for (int i = 0; i < n; i++) {
        g_ps_map[i].sdk_port = sdk_ports[i];
        memcpy(g_ps_map[i].name, names[i], BCMCMD_PORT_NAME_LEN);
        g_ps_map[i].name[BCMCMD_PORT_NAME_LEN - 1] = '\0';
    }
    syslog(LOG_INFO, "%s: ps map: %d ports", DAEMON_NAME, n);
    return 0;
}

/* ---- Redis connections (persistent, reconnect on error) ---- */

static redisContext *g_rdb_counters = NULL;  /* DB 2 */
static redisContext *g_rdb_config   = NULL;  /* DB 4 */
static redisContext *g_rdb_flex     = NULL;  /* DB 5 */

static redisContext *redis_connect(int db)
{
    struct timeval tv = { .tv_sec = 0, .tv_usec = REDIS_TIMEOUT_MS * 1000 };
    redisContext *c = redisConnectWithTimeout("127.0.0.1", 6379, tv);
    if (!c || c->err) {
        if (c) redisFree(c);
        return NULL;
    }
    redisReply *r = redisCommand(c, "SELECT %d", db);
    if (r) freeReplyObject(r);
    return c;
}

static int redis_ensure(void)
{
    if (!g_rdb_counters) g_rdb_counters = redis_connect(2);
    if (!g_rdb_config)   g_rdb_config   = redis_connect(4);
    if (!g_rdb_flex)     g_rdb_flex     = redis_connect(5);
    return (g_rdb_counters && g_rdb_config && g_rdb_flex) ? 0 : -1;
}

static void redis_disconnect(void)
{
    if (g_rdb_counters) { redisFree(g_rdb_counters); g_rdb_counters = NULL; }
    if (g_rdb_config)   { redisFree(g_rdb_config);   g_rdb_config   = NULL; }
    if (g_rdb_flex)     { redisFree(g_rdb_flex);     g_rdb_flex     = NULL; }
}

static void redis_consume_reply(redisContext *c)
{
    redisReply *r = NULL;
    if (redisGetReply(c, (void **)&r) == REDIS_OK && r)
        freeReplyObject(r);
}

/* ---- port resolution ---- */

/* Resolve Ethernet name -> bcmcmd port name via CONFIG_DB lanes.
 * Sets *n_lanes. Returns port name or NULL. */
static const char *resolve_port(const char *eth_name, int *n_lanes)
{
    *n_lanes = 0;
    redisReply *r = redisCommand(g_rdb_config, "HGET PORT|%s lanes", eth_name);
    if (!r || r->type != REDIS_REPLY_STRING || !r->str) {
        if (r) freeReplyObject(r);
        return NULL;
    }
    *n_lanes = count_lanes(r->str);
    uint32_t first_lane = (uint32_t)atoi(r->str);
    freeReplyObject(r);

    int sdk = sdk_port_for_lane(first_lane);
    if (sdk < 0) return NULL;
    return port_name_for_sdk(sdk);
}

/* ---- flex port tracking ---- */

static flex_port_t *find_flex(const char *eth_name)
{
    for (int i = 0; i < g_n_flex; i++)
        if (strcmp(g_flex[i].eth_name, eth_name) == 0)
            return &g_flex[i];
    return NULL;
}

/* Refresh breakout port list from COUNTERS_PORT_NAME_MAP.
 * Returns 1 if any OID changed (syncd restart), 0 otherwise. */
static int refresh_flex_ports(void)
{
    redisReply *map = redisCommand(g_rdb_counters, "HGETALL COUNTERS_PORT_NAME_MAP");
    if (!map || map->type != REDIS_REPLY_ARRAY) {
        if (map) freeReplyObject(map);
        return 0;
    }

    int oid_changed = 0;

    for (size_t i = 0; i + 1 < map->elements; i += 2) {
        const char *eth = map->element[i]->str;
        const char *oid = map->element[i + 1]->str;
        if (!eth || !oid) continue;

        int n_lanes = 0;
        const char *pname = resolve_port(eth, &n_lanes);
        if (!pname || n_lanes >= 4) continue;  /* not a breakout port */

        flex_port_t *fp = find_flex(eth);
        if (fp) {
            /* Check for OID change (syncd restart). */
            if (strcmp(fp->oid, oid) != 0) {
                syslog(LOG_INFO, "%s: OID changed: %s %s -> %s",
                       DAEMON_NAME, eth, fp->oid, oid);
                strncpy(fp->oid, oid, sizeof(fp->oid) - 1);
                fp->flex_removed = 0;
                fp->rate_state = 0;
                fp->zero_cycles = 0;
                memset(&fp->smooth_rx_bps, 0, 4 * sizeof(double));
                oid_changed = 1;
            }
        } else {
            /* New breakout port. */
            if (g_n_flex >= MAX_FLEX_PORTS) continue;
            fp = &g_flex[g_n_flex++];
            memset(fp, 0, sizeof(*fp));
            strncpy(fp->eth_name, eth, sizeof(fp->eth_name) - 1);
            strncpy(fp->oid, oid, sizeof(fp->oid) - 1);
            strncpy(fp->bcm_name, pname, BCMCMD_PORT_NAME_LEN - 1);
            fp->n_lanes = n_lanes;
            oid_changed = 1;
            syslog(LOG_INFO, "%s: breakout port %s oid=%s bcm=%s lanes=%d",
                   DAEMON_NAME, eth, oid, pname, n_lanes);
        }
    }

    freeReplyObject(map);
    return oid_changed;
}

/* Remove flex ports from FLEX_COUNTER_TABLE (DB 5).
 * Called every cycle because orchagent re-populates DB 5 periodically. */
static void remove_from_flex_counter(void)
{
    int removed = 0;
    for (int i = 0; i < g_n_flex; i++) {
        flex_port_t *fp = &g_flex[i];
        char key[160];
        snprintf(key, sizeof(key),
                 "FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:%s", fp->oid);
        redisReply *r = redisCommand(g_rdb_flex, "DEL %s", key);
        if (r) {
            if (r->type == REDIS_REPLY_INTEGER && r->integer > 0)
                removed++;
            freeReplyObject(r);
        }
    }
    if (removed)
        syslog(LOG_INFO, "%s: removed %d breakout ports from FlexCounter",
               DAEMON_NAME, removed);
}

/* ---- counter + rate writes ---- */

/* Find counter cache row for a BCM port name. */
static port_row_t *find_cache_row(const char *bcm_name)
{
    for (int i = 0; i < g_cache.n_rows; i++)
        if (strcmp(g_cache.rows[i].port_name, bcm_name) == 0)
            return &g_cache.rows[i];
    return NULL;
}

/* Write all SAI stat fields for a flex port to COUNTERS:<oid>. */
static void write_counters(flex_port_t *fp, const port_row_t *row)
{
    char cmd[8192];
    int pos = snprintf(cmd, sizeof(cmd), "HMSET COUNTERS:%s", fp->oid);

    for (int i = 0; i < g_stat_map_size && pos < (int)sizeof(cmd) - 128; i++) {
        const char *field = sai_stat_field_name(g_stat_map[i].stat_id);
        if (!field) continue;
        pos += snprintf(cmd + pos, sizeof(cmd) - (size_t)pos,
                        " %s %lu", field, (unsigned long)row->val[i]);
    }

    redisAppendCommand(g_rdb_counters, cmd);
}

/* Compute EWMA rates and write to RATES:<oid>. */
static void write_rates(flex_port_t *fp, const port_row_t *row)
{
    uint64_t in_oct  = row->val[IDX_IN_OCTETS];
    uint64_t out_oct = row->val[IDX_OUT_OCTETS];
    uint64_t in_uc   = row->val[IDX_IN_UCAST_PKTS];
    uint64_t in_nuc  = row->val[IDX_IN_NON_UCAST_PKTS];
    uint64_t out_uc  = row->val[IDX_OUT_UCAST_PKTS];
    uint64_t out_nuc = row->val[IDX_OUT_NON_UCAST_PKTS];

    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);

    if (fp->rate_state >= 1) {
        /* Have previous values — compute rates. */
        double delta_s = (double)(now.tv_sec - fp->last_time.tv_sec)
                       + (double)(now.tv_nsec - fp->last_time.tv_nsec) / 1e9;
        if (delta_s <= 0) delta_s = POLL_INTERVAL_S;

        /* Guard against uint64 underflow when hardware counters are reset
         * (e.g. syncd restart after DPB resets BCM port counters to 0).
         * A naive (double)(a - b) with a < b wraps to ~1.84e19 and produces
         * a multi-TB/s spike — positive, so no signed-clamp catches it.
         * Compare before subtracting; emit 0 for that cycle on counter reset. */
        double rx_bps = (in_oct  >= fp->last_in_oct)  ?
            (double)(in_oct  - fp->last_in_oct)  / delta_s : 0.0;
        double tx_bps = (out_oct >= fp->last_out_oct) ?
            (double)(out_oct - fp->last_out_oct) / delta_s : 0.0;

        uint64_t in_pkts      = in_uc  + in_nuc;
        uint64_t last_in_pkts = fp->last_in_ucast  + fp->last_in_non_ucast;
        uint64_t out_pkts     = out_uc + out_nuc;
        uint64_t last_out_pkts= fp->last_out_ucast + fp->last_out_non_ucast;

        double rx_pps = (in_pkts  >= last_in_pkts)  ?
            (double)(in_pkts  - last_in_pkts)  / delta_s : 0.0;
        double tx_pps = (out_pkts >= last_out_pkts) ?
            (double)(out_pkts - last_out_pkts) / delta_s : 0.0;

        if (fp->rate_state >= 2) {
            /* EWMA smoothing. */
            fp->smooth_rx_bps = RATES_ALPHA * rx_bps + (1 - RATES_ALPHA) * fp->smooth_rx_bps;
            fp->smooth_tx_bps = RATES_ALPHA * tx_bps + (1 - RATES_ALPHA) * fp->smooth_tx_bps;
            fp->smooth_rx_pps = RATES_ALPHA * rx_pps + (1 - RATES_ALPHA) * fp->smooth_rx_pps;
            fp->smooth_tx_pps = RATES_ALPHA * tx_pps + (1 - RATES_ALPHA) * fp->smooth_tx_pps;
        } else {
            /* First rate sample — no smoothing. */
            fp->smooth_rx_bps = rx_bps;
            fp->smooth_tx_bps = tx_bps;
            fp->smooth_rx_pps = rx_pps;
            fp->smooth_tx_pps = tx_pps;
            fp->rate_state = 2;
        }

        redisAppendCommand(g_rdb_counters,
            "HMSET RATES:%s "
            "RX_BPS %f TX_BPS %f RX_PPS %f TX_PPS %f "
            "SAI_PORT_STAT_IF_IN_OCTETS_last %lu "
            "SAI_PORT_STAT_IF_OUT_OCTETS_last %lu "
            "SAI_PORT_STAT_IF_IN_UCAST_PKTS_last %lu "
            "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS_last %lu "
            "SAI_PORT_STAT_IF_OUT_UCAST_PKTS_last %lu "
            "SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS_last %lu",
            fp->oid,
            fp->smooth_rx_bps, fp->smooth_tx_bps,
            fp->smooth_rx_pps, fp->smooth_tx_pps,
            (unsigned long)in_oct, (unsigned long)out_oct,
            (unsigned long)in_uc, (unsigned long)in_nuc,
            (unsigned long)out_uc, (unsigned long)out_nuc);

        redisAppendCommand(g_rdb_counters,
            "HMSET RATES:%s:PORT INIT_DONE DONE", fp->oid);
    } else {
        /* First cycle: store _last values only, no rates.
         *
         * Guard against BCM transient zero: during DPB port reconfiguration
         * the BCM shell briefly reports 0 bytes for unrelated ports.  If we
         * commit 0 as the baseline here, the next cycle (with the real ~40 GB
         * accumulated value) produces a physically impossible rate spike
         * (e.g. 5934 MB/s on a 25G link) because delta = 40 GB / 3 s.
         *
         * Instead, treat in_oct==0 as a missed cycle: extend the waiting
         * window (update last_time so the eventual delta_s is correct) and
         * try again next poll.  The real accumulated value will appear once
         * BCM finishes the port reconfiguration. */
        if (in_oct == 0) {
            fp->zero_cycles++;
            fp->last_time = now;   /* keep window current for when data returns */
            syslog(LOG_DEBUG, "%s: %s zero_cycle=%d, deferring baseline",
                   DAEMON_NAME, fp->eth_name, fp->zero_cycles);
            return;
        }

        redisAppendCommand(g_rdb_counters,
            "HMSET RATES:%s "
            "RX_BPS 0 TX_BPS 0 RX_PPS 0 TX_PPS 0 "
            "SAI_PORT_STAT_IF_IN_OCTETS_last %lu "
            "SAI_PORT_STAT_IF_OUT_OCTETS_last %lu "
            "SAI_PORT_STAT_IF_IN_UCAST_PKTS_last %lu "
            "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS_last %lu "
            "SAI_PORT_STAT_IF_OUT_UCAST_PKTS_last %lu "
            "SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS_last %lu",
            fp->oid,
            (unsigned long)in_oct, (unsigned long)out_oct,
            (unsigned long)in_uc, (unsigned long)in_nuc,
            (unsigned long)out_uc, (unsigned long)out_nuc);

        redisAppendCommand(g_rdb_counters,
            "HMSET RATES:%s:PORT INIT_DONE COUNTERS_LAST", fp->oid);

        if (fp->zero_cycles > 0)
            syslog(LOG_INFO, "%s: %s baseline captured after %d zero cycle(s)",
                   DAEMON_NAME, fp->eth_name, fp->zero_cycles);
        fp->zero_cycles = 0;
        fp->rate_state = 1;
    }

    /* Update last-seen values. */
    fp->last_time       = now;
    fp->last_in_oct     = in_oct;
    fp->last_out_oct    = out_oct;
    fp->last_in_ucast   = in_uc;
    fp->last_in_non_ucast = in_nuc;
    fp->last_out_ucast  = out_uc;
    fp->last_out_non_ucast = out_nuc;
}

/* Write zero-valued stat keys for a port absent from 'show c all' (link-down).
 * Only writes if COUNTERS:<oid> has fewer fields than STAT_MAP. */
static void write_zero_stats(flex_port_t *fp)
{
    redisReply *r = redisCommand(g_rdb_counters, "HLEN COUNTERS:%s", fp->oid);
    if (r && r->type == REDIS_REPLY_INTEGER && r->integer >= g_stat_map_size) {
        freeReplyObject(r);
        return;
    }
    if (r) freeReplyObject(r);

    char cmd[8192];
    int pos = snprintf(cmd, sizeof(cmd), "HMSET COUNTERS:%s", fp->oid);
    for (int i = 0; i < g_stat_map_size && pos < (int)sizeof(cmd) - 128; i++) {
        const char *field = sai_stat_field_name(g_stat_map[i].stat_id);
        if (!field) continue;
        pos += snprintf(cmd + pos, sizeof(cmd) - (size_t)pos, " %s 0", field);
    }
    redisReply *wr = redisCommand(g_rdb_counters, cmd);
    if (wr) freeReplyObject(wr);
}

/* ---- main loop ---- */

int main(void)
{
    openlog(DAEMON_NAME, LOG_PID | LOG_NDELAY, LOG_DAEMON);
    signal(SIGTERM, sig_handler);
    signal(SIGINT,  sig_handler);

    /* Socket path: env override, else try host then container. */
    const char *env_sock = getenv("BCMCMD_SOCKET");
    if (env_sock) {
        g_socket_path = env_sock;
    } else if (access(BCMCMD_SOCKET_HOST, F_OK) == 0) {
        g_socket_path = BCMCMD_SOCKET_HOST;
    } else {
        g_socket_path = BCMCMD_SOCKET_CONTAINER;
    }

    /* BCM config: env var or auto-detect. */
    const char *bcm_cfg = getenv("WEDGE100S_BCM_CONFIG");
    if (!bcm_cfg)
        bcm_cfg = auto_detect_bcm_config();
    if (bcm_cfg)
        parse_bcm_config(bcm_cfg);
    else
        syslog(LOG_WARNING, "%s: no BCM config found", DAEMON_NAME);

    memset(&g_cache, 0, sizeof(g_cache));

    /* Initial ps map — may fail if bcmcmd not ready yet. */
    refresh_ps_map();

    int bcmcmd_warned = 0;
    int redis_warned = 0;

    syslog(LOG_INFO, "%s: starting (poll=%ds, alpha=%.2f, socket=%s)",
           DAEMON_NAME, POLL_INTERVAL_S, RATES_ALPHA, g_socket_path);

    while (g_running) {
        sleep(POLL_INTERVAL_S);
        if (!g_running) break;

        /* Retry ps map if it failed at startup. */
        if (g_ps_map_size == 0) {
            refresh_ps_map();
            if (g_ps_map_size == 0) continue;
        }

        /* Ensure Redis connections. */
        if (redis_ensure() < 0) {
            if (!redis_warned) {
                syslog(LOG_WARNING, "%s: Redis unavailable, will retry", DAEMON_NAME);
                redis_warned = 1;
            }
            redis_disconnect();
            continue;
        }
        if (redis_warned) {
            syslog(LOG_INFO, "%s: Redis connected", DAEMON_NAME);
            redis_warned = 0;
        }

        /* Refresh breakout port list from Redis (dynamic — adapts to DPB). */
        refresh_flex_ports();
        if (g_n_flex == 0) continue;

        /* Remove breakout ports from FlexCounter every cycle.
         * orchagent re-populates FLEX_COUNTER_TABLE periodically, so a
         * one-time removal is insufficient — we must continuously enforce. */
        remove_from_flex_counter();

        /* Build BCM port name list for targeted query. */
        char query_ports[MAX_FLEX_PORTS][BCMCMD_PORT_NAME_LEN];
        int n_query = 0;
        for (int i = 0; i < g_n_flex && n_query < MAX_FLEX_PORTS; i++)
            memcpy(query_ports[n_query++], g_flex[i].bcm_name,
                   BCMCMD_PORT_NAME_LEN);

        /* Connect to bcmcmd and fetch counters.  Use per-port queries
         * when few breakout ports exist (fast, low I/O), but fall back
         * to bulk 'show c all' when many ports are broken out. */
        int fd = bcmcmd_connect(g_socket_path, CONNECT_TIMEOUT_MS);
        if (fd < 0) {
            if (!bcmcmd_warned) {
                syslog(LOG_WARNING, "%s: bcmcmd unavailable, will retry", DAEMON_NAME);
                bcmcmd_warned = 1;
            }
            continue;
        }
        if (bcmcmd_warned) {
            syslog(LOG_INFO, "%s: bcmcmd connected", DAEMON_NAME);
            bcmcmd_warned = 0;
        }

        int fetch_rc;
        if (n_query <= PER_PORT_THRESHOLD) {
            fetch_rc = bcmcmd_fetch_port_counters(fd, &g_cache,
                            (const char (*)[BCMCMD_PORT_NAME_LEN])query_ports,
                            n_query);
        } else {
            fetch_rc = bcmcmd_fetch_counters(fd, &g_cache);
        }
        bcmcmd_close(fd);
        if (fetch_rc < 0) continue;

        /* Write counters and rates for each breakout port. */
        int pending = 0;
        for (int i = 0; i < g_n_flex; i++) {
            flex_port_t *fp = &g_flex[i];
            port_row_t *row = find_cache_row(fp->bcm_name);
            if (row) {
                write_counters(fp, row);  /* appends 1 reply */
                write_rates(fp, row);     /* appends 2 replies */
                pending += 3;
            } else {
                write_zero_stats(fp);     /* synchronous */
            }
        }

        /* Drain pipelined replies. */
        for (int i = 0; i < pending; i++)
            redis_consume_reply(g_rdb_counters);

        /* Check for Redis errors (pipeline may have caused disconnect). */
        if (g_rdb_counters->err) {
            syslog(LOG_WARNING, "%s: Redis error: %s, reconnecting",
                   DAEMON_NAME, g_rdb_counters->errstr);
            redis_disconnect();
            /* Reset rate state so we don't compute bogus rates
             * across a reconnection gap. */
            for (int i = 0; i < g_n_flex; i++)
                g_flex[i].rate_state = 0;
        }
    }

    redis_disconnect();
    syslog(LOG_INFO, "%s: shutting down", DAEMON_NAME);
    closelog();
    return 0;
}
