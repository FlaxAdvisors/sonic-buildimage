/* daemon.c — Flex counter daemon for Wedge100S-32X.
 *
 * Polls bcmcmd 'show counters' every 3s, accumulates per-port deltas,
 * identifies flex sub-ports by COUNTERS_DB key count (<=2 = flex),
 * and writes all 66 SAI stat fields to COUNTERS_DB via Redis HMSET.
 *
 * OID-to-port resolution chain:
 *   COUNTERS_PORT_NAME_MAP (DB 2) -> Ethernet name -> SAI OID
 *   CONFIG_DB PORT|EthernetN (DB 4) -> lanes field
 *   BCM config portmap -> lane -> SDK port number
 *   bcmcmd ps -> SDK port number -> port name (xe86, ce0, ...)
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
#include <hiredis/hiredis.h>

#define DAEMON_NAME        "flex-counter-daemon"
#define POLL_INTERVAL_S    3
#define CONNECT_TIMEOUT_MS 50
#define MAX_LANE_MAP       512
#define MAX_PS_MAP         256
#define REDIS_TIMEOUT_MS   500

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

/* ---- bcmcmd ps map refresh ---- */

static int refresh_ps_map(void)
{
    int fd = bcmcmd_connect(BCMCMD_SOCKET_PATH, CONNECT_TIMEOUT_MS);
    if (fd < 0) return -1;

    int sdk_ports[BCMCMD_MAX_PORTS];
    char names[BCMCMD_MAX_PORTS][BCMCMD_PORT_NAME_LEN];
    int n = bcmcmd_ps(fd, sdk_ports, names, BCMCMD_MAX_PORTS);
    bcmcmd_close(fd);

    if (n < 0) return -1;

    g_ps_map_size = n;
    for (int i = 0; i < n; i++) {
        g_ps_map[i].sdk_port = sdk_ports[i];
        strncpy(g_ps_map[i].name, names[i], BCMCMD_PORT_NAME_LEN - 1);
        g_ps_map[i].name[BCMCMD_PORT_NAME_LEN - 1] = '\0';
    }
    syslog(LOG_INFO, "%s: ps map refreshed (%d ports)", DAEMON_NAME, n);
    return 0;
}

/* ---- Redis helpers ---- */

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

/* Count lanes in a comma-separated lane string (e.g. "117" = 1, "5,6,7,8" = 4). */
static int count_lanes(const char *lanes_str)
{
    int count = 1;
    for (const char *p = lanes_str; *p; p++)
        if (*p == ',') count++;
    return count;
}

/* Resolve Ethernet port name -> bcmcmd port name via CONFIG_DB lanes -> lane_map -> ps_map.
 * Sets *n_lanes to the number of lanes for this port.
 * Returns NULL if resolution fails. */
static const char *resolve_port(redisContext *cfg_db, const char *eth_name, int *n_lanes)
{
    *n_lanes = 0;

    /* Get lanes from CONFIG_DB PORT|EthernetN */
    redisReply *r = redisCommand(cfg_db, "HGET PORT|%s lanes", eth_name);
    if (!r || r->type != REDIS_REPLY_STRING || !r->str) {
        if (r) freeReplyObject(r);
        return NULL;
    }

    *n_lanes = count_lanes(r->str);

    /* lanes is a comma-separated list; use the first lane for port lookup. */
    uint32_t first_lane = (uint32_t)atoi(r->str);
    freeReplyObject(r);

    int sdk_port = sdk_port_for_lane(first_lane);
    if (sdk_port < 0) return NULL;

    return port_name_for_sdk(sdk_port);
}

/* Write all 66 SAI stat fields for a flex OID using HMSET.
 * Values come from the counter cache row. */
static void write_counters(redisContext *c, const char *oid, const port_row_t *row)
{
    /* Build HMSET command with all stat fields.
     * Format: HMSET COUNTERS:<oid> field1 val1 field2 val2 ... */
    char cmd[8192];
    int pos = snprintf(cmd, sizeof(cmd), "HMSET COUNTERS:%s", oid);

    for (int i = 0; i < g_stat_map_size && pos < (int)sizeof(cmd) - 128; i++) {
        const char *field = sai_stat_field_name(g_stat_map[i].stat_id);
        if (!field) continue;
        pos += snprintf(cmd + pos, sizeof(cmd) - (size_t)pos,
                        " %s %lu", field, (unsigned long)row->val[i]);
    }

    redisReply *r = redisCommand(c, cmd);
    if (r) freeReplyObject(r);
}

/* ---- main loop ---- */

int main(void)
{
    openlog(DAEMON_NAME, LOG_PID | LOG_NDELAY, LOG_DAEMON);
    signal(SIGTERM, sig_handler);
    signal(SIGINT,  sig_handler);

    /* Parse BCM config. */
    const char *bcm_cfg = getenv("WEDGE100S_BCM_CONFIG");
    if (bcm_cfg)
        parse_bcm_config(bcm_cfg);
    else
        syslog(LOG_WARNING, "%s: WEDGE100S_BCM_CONFIG not set", DAEMON_NAME);

    /* Init counter cache. */
    memset(&g_cache, 0, sizeof(g_cache));
    pthread_mutex_init(&g_cache.lock, NULL);

    /* Initial ps map — may fail if bcmcmd not ready yet; retried in loop. */
    refresh_ps_map();

    int bcmcmd_warn_logged = 0;
    int redis_warn_logged = 0;
    int ps_map_retries = 0;

    syslog(LOG_INFO, "%s: starting (poll interval %ds)", DAEMON_NAME, POLL_INTERVAL_S);

    while (g_running) {
        sleep(POLL_INTERVAL_S);
        if (!g_running) break;

        /* Retry ps map if it failed at startup. */
        if (g_ps_map_size == 0 && ps_map_retries < 10) {
            refresh_ps_map();
            ps_map_retries++;
        }

        /* Connect to bcmcmd, fetch counters, disconnect. */
        int fd = bcmcmd_connect(BCMCMD_SOCKET_PATH, CONNECT_TIMEOUT_MS);
        if (fd < 0) {
            if (!bcmcmd_warn_logged) {
                syslog(LOG_WARNING, "%s: bcmcmd socket unavailable, will retry",
                       DAEMON_NAME);
                bcmcmd_warn_logged = 1;
            }
            continue;
        }
        if (bcmcmd_warn_logged) {
            syslog(LOG_INFO, "%s: bcmcmd connected", DAEMON_NAME);
            bcmcmd_warn_logged = 0;
        }

        /* If ps map is still empty, try via this connection. */
        if (g_ps_map_size == 0) {
            int sdk_ports[BCMCMD_MAX_PORTS];
            char names[BCMCMD_MAX_PORTS][BCMCMD_PORT_NAME_LEN];
            int n = bcmcmd_ps(fd, sdk_ports, names, BCMCMD_MAX_PORTS);
            if (n > 0) {
                g_ps_map_size = n;
                for (int i = 0; i < n; i++) {
                    g_ps_map[i].sdk_port = sdk_ports[i];
                    strncpy(g_ps_map[i].name, names[i], BCMCMD_PORT_NAME_LEN - 1);
                    g_ps_map[i].name[BCMCMD_PORT_NAME_LEN - 1] = '\0';
                }
                syslog(LOG_INFO, "%s: late ps fetch got %d ports", DAEMON_NAME, n);
            }
        }

        bcmcmd_fetch_counters(fd, &g_cache);
        bcmcmd_close(fd);

        /* Connect to Redis COUNTERS_DB (2) and CONFIG_DB (4). */
        redisContext *cdb = redis_connect(2);
        redisContext *cfgdb = redis_connect(4);
        if (!cdb || !cfgdb) {
            if (!redis_warn_logged) {
                syslog(LOG_WARNING, "%s: Redis unavailable, will retry", DAEMON_NAME);
                redis_warn_logged = 1;
            }
            if (cdb) redisFree(cdb);
            if (cfgdb) redisFree(cfgdb);
            continue;
        }
        if (redis_warn_logged) {
            syslog(LOG_INFO, "%s: Redis connected", DAEMON_NAME);
            redis_warn_logged = 0;
        }

        /* Read COUNTERS_PORT_NAME_MAP: Ethernet name -> OID. */
        redisReply *map_reply = redisCommand(cdb, "HGETALL COUNTERS_PORT_NAME_MAP");
        if (!map_reply || map_reply->type != REDIS_REPLY_ARRAY) {
            if (map_reply) freeReplyObject(map_reply);
            redisFree(cdb);
            redisFree(cfgdb);
            continue;
        }

        /* For each port in the map, check if it's a flex sub-port. */
        for (size_t i = 0; i + 1 < map_reply->elements; i += 2) {
            const char *eth_name = map_reply->element[i]->str;
            const char *oid      = map_reply->element[i + 1]->str;
            if (!eth_name || !oid) continue;

            /* Resolve Ethernet -> bcmcmd port name and get lane count.
             * Flex sub-ports have fewer than 4 lanes (breakout from 100G). */
            int n_lanes = 0;
            const char *pname = resolve_port(cfgdb, eth_name, &n_lanes);
            if (!pname) continue;
            if (n_lanes >= 4) continue;  /* native 100G port — skip */

            /* Find the port in the counter cache. */
            pthread_mutex_lock(&g_cache.lock);
            port_row_t *row = NULL;
            for (int r = 0; r < g_cache.n_rows; r++) {
                if (strcmp(g_cache.rows[r].port_name, pname) == 0) {
                    row = &g_cache.rows[r];
                    break;
                }
            }
            if (row) {
                write_counters(cdb, oid, row);
            }
            pthread_mutex_unlock(&g_cache.lock);
        }

        freeReplyObject(map_reply);
        redisFree(cdb);
        redisFree(cfgdb);
    }

    syslog(LOG_INFO, "%s: shutting down", DAEMON_NAME);
    closelog();
    return 0;
}
