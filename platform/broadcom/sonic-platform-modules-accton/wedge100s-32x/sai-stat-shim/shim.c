/* shim.c — LD_PRELOAD intercept for sai_api_query(SAI_API_PORT).
 *
 * Entry point: sai_api_query() — overrides the symbol in libsai.so.1.0.
 * When syncd calls sai_api_query(SAI_API_PORT, &port_api):
 *   1. Call the real sai_api_query (via RTLD_NEXT) to get real function pointers.
 *   2. Save the real get_port_stats pointer.
 *   3. Replace get_port_stats and get_port_stats_ext with shim functions.
 *   4. Return the modified port_api struct.
 *
 * BCM config parse: portmap_<SDK_port>.0=<physical_lane>:<speed>[:<flags>]
 * Maps physical_lane → sdk_port.  Lane IDs in HW_LANE_LIST match CONFIG_DB lanes field.
 *
 * Flex detection (on first call per OID):
 *   Call real get_port_stats. If SUCCESS → non-flex, cache result, return.
 *   If non-SUCCESS → flex; query SAI_PORT_ATTR_HW_LANE_LIST; map lane → sdk_port
 *   via g_lane_map[]; map sdk_port → port_name via ps_map[]; use bcmcmd cache.
 */
#define _GNU_SOURCE
#include "shim.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <syslog.h>
#include <unistd.h>
#include <pthread.h>
#include <time.h>
#include <sys/mman.h>

/* ---- globals ---- */

static sai_get_port_stats_fn     g_real_get_port_stats     = NULL;
static sai_get_port_stats_ext_fn g_real_get_port_stats_ext = NULL;
static sai_get_port_attribute_fn g_real_get_port_attr      = NULL;

/* OID classification cache. */
static oid_cache_t g_oids;

/* Counter value cache. */
static counter_cache_t g_cache;

/* bcmcmd socket fd (-1 = not connected). */
static int g_bcmfd = -1;
static pthread_mutex_t g_bcmfd_lock = PTHREAD_MUTEX_INITIALIZER;

/* Port name table from 'ps': sdk_port → port_name */
static struct { int sdk_port; char name[SHIM_PORT_NAME_LEN]; } g_ps_map[SHIM_MAX_PORTS];
static int g_ps_map_size = 0;

/* Lane → SDK port map from BCM config. */
lane_map_entry_t g_lane_map[SHIM_MAX_LANE_MAP];
int              g_lane_map_size = 0;

/* Initialised flag. */
static int g_initialised = 0;

/* ---- BCM config parser ---- */

/* Parse portmap lines from the BCM config file.
 * Format: portmap_<SDK_port>.0=<physical_lane>:<speed>[:<flags>]
 * Populates g_lane_map[]. */
static void parse_bcm_config(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        syslog(LOG_WARNING, "shim: cannot open BCM config '%s': %m", path);
        return;
    }
    char line[256];
    while (fgets(line, sizeof(line), f) && g_lane_map_size < SHIM_MAX_LANE_MAP) {
        /* Match: portmap_<N>.0=<lane>:<speed>[:<flags>] */
        int sdk_port, phys_lane, speed;
        /* We only need .0 entries (primary sub-port). */
        if (sscanf(line, "portmap_%d.0=%d:%d", &sdk_port, &phys_lane, &speed) == 3) {
            g_lane_map[g_lane_map_size].physical_lane = (uint32_t)phys_lane;
            g_lane_map[g_lane_map_size].sdk_port      = sdk_port;
            g_lane_map_size++;
        }
    }
    fclose(f);
    syslog(LOG_INFO, "shim: parsed %d lane→sdk_port entries from %s",
           g_lane_map_size, path);
}

/* ---- SDK port lookup helpers ---- */

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

/* ---- bcmcmd connection management ---- */

/* Connect to bcmcmd socket, run 'ps', populate g_ps_map.
 * Returns the connected fd or -1. */
static int bcmcmd_init(void)
{
    const char *sock = SHIM_SOCKET_PATH;
    int fd = bcmcmd_connect(sock, SHIM_CONNECT_TIMEOUT_MS);
    if (fd < 0) {
        syslog(LOG_WARNING, "shim: cannot connect to bcmcmd socket %s: %m", sock);
        return -1;
    }

    /* Build sdk_port → port_name table. */
    int sdk_ports[SHIM_MAX_PORTS];
    char names[SHIM_MAX_PORTS][SHIM_PORT_NAME_LEN];
    int n = bcmcmd_ps(fd, sdk_ports, names, SHIM_MAX_PORTS);
    if (n < 0) {
        syslog(LOG_WARNING, "shim: bcmcmd 'ps' failed");
        bcmcmd_close(fd);
        return -1;
    }
    g_ps_map_size = n;
    for (int i = 0; i < n; i++) {
        g_ps_map[i].sdk_port = sdk_ports[i];
        strncpy(g_ps_map[i].name, names[i], SHIM_PORT_NAME_LEN - 1);
    }
    syslog(LOG_INFO, "shim: bcmcmd connected, %d ports from ps", n);
    return fd;
}

/* ---- cache staleness check ---- */

static int cache_is_stale(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    long diff_ms = (now.tv_sec  - g_cache.fetched_at.tv_sec)  * 1000 +
                   (now.tv_nsec - g_cache.fetched_at.tv_nsec) / 1000000;
    return diff_ms >= SHIM_CACHE_TTL_MS;
}

/* Refresh counter cache if stale.  Acquires/releases g_bcmfd_lock internally. */
static void refresh_cache_if_stale(void)
{
    pthread_mutex_lock(&g_cache.lock);
    if (!cache_is_stale() || g_cache.fetch_in_progress) {
        pthread_mutex_unlock(&g_cache.lock);
        return;
    }
    g_cache.fetch_in_progress = 1;
    pthread_mutex_unlock(&g_cache.lock);

    pthread_mutex_lock(&g_bcmfd_lock);
    if (g_bcmfd < 0)
        g_bcmfd = bcmcmd_init();
    if (g_bcmfd >= 0) {
        if (bcmcmd_fetch_counters(g_bcmfd, &g_cache) < 0) {
            bcmcmd_close(g_bcmfd);
            g_bcmfd = -1;
        }
    }
    pthread_mutex_unlock(&g_bcmfd_lock);

    pthread_mutex_lock(&g_cache.lock);
    g_cache.fetch_in_progress = 0;
    pthread_mutex_unlock(&g_cache.lock);
}

/* ---- OID cache helpers ---- */

static oid_entry_t *oid_find(sai_object_id_t oid)
{
    for (int i = 0; i < g_oids.n; i++)
        if (g_oids.e[i].valid && g_oids.e[i].oid == oid)
            return &g_oids.e[i];
    return NULL;
}

static oid_entry_t *oid_insert(sai_object_id_t oid, int is_flex, int sdk_port)
{
    if (g_oids.n >= SHIM_MAX_OID_CACHE) return NULL;
    oid_entry_t *e = &g_oids.e[g_oids.n++];
    e->oid      = oid;
    e->is_flex  = is_flex;
    e->sdk_port = sdk_port;
    e->valid    = 1;
    return e;
}

/* ---- Port info resolution for an unknown OID ---- */

/* Query HW_LANE_LIST via real SAI, map first matching lane to sdk_port.
 * Returns sdk_port or -1. */
static int resolve_sdk_port(sai_object_id_t oid)
{
    uint32_t lane_buf[8] = {0};
    sai_u32_list_t lane_list = { .count = 8, .list = lane_buf };
    sai_attribute_t attr;
    attr.id = SAI_PORT_ATTR_HW_LANE_LIST;
    attr.value.u32list = lane_list;

    if (!g_real_get_port_attr) return -1;
    if (g_real_get_port_attr(oid, 1, &attr) != SAI_STATUS_SUCCESS) return -1;

    for (uint32_t i = 0; i < 8 && i < attr.value.u32list.count; i++) {
        int sp = sdk_port_for_lane(attr.value.u32list.list[i]);
        if (sp >= 0) return sp;
    }
    return -1;
}

/* ---- Shim get_port_stats ---- */

static sai_status_t shim_get_port_stats(
    sai_object_id_t  port_id,
    uint32_t         count,
    const uint32_t  *ids,
    uint64_t        *values)
{
    pthread_mutex_lock(&g_oids.lock);
    oid_entry_t *entry = oid_find(port_id);
    pthread_mutex_unlock(&g_oids.lock);

    if (entry && !entry->is_flex) {
        /* Known non-flex: passthrough. */
        return g_real_get_port_stats(port_id, count, ids, values);
    }

    if (!entry) {
        /* First call for this OID: try real function to classify. */
        sai_status_t st = g_real_get_port_stats(port_id, count, ids, values);
        if (st == SAI_STATUS_SUCCESS) {
            /* Non-flex: cache and return real result. */
            pthread_mutex_lock(&g_oids.lock);
            oid_insert(port_id, 0, -1);
            pthread_mutex_unlock(&g_oids.lock);
            return st;
        }
        /* Flex: resolve sdk_port and cache. */
        int sdk_port = resolve_sdk_port(port_id);
        if (sdk_port < 0) {
            /* Cannot identify port; return zeros + SUCCESS so COUNTERS_DB gets keys. */
            memset(values, 0, count * sizeof(uint64_t));
            return SAI_STATUS_SUCCESS;
        }
        pthread_mutex_lock(&g_oids.lock);
        entry = oid_insert(port_id, 1, sdk_port);
        pthread_mutex_unlock(&g_oids.lock);
        if (!entry) {
            memset(values, 0, count * sizeof(uint64_t));
            return SAI_STATUS_SUCCESS;
        }
    }

    /* Flex path: use bcmcmd counter cache. */
    refresh_cache_if_stale();

    /* Find the port_name for this sdk_port. */
    const char *pname = port_name_for_sdk(entry->sdk_port);

    pthread_mutex_lock(&g_cache.lock);
    port_row_t *row = NULL;
    if (pname) {
        for (int i = 0; i < g_cache.n_rows; i++) {
            if (strcmp(g_cache.rows[i].port_name, pname) == 0) {
                row = &g_cache.rows[i];
                break;
            }
        }
    }
    for (uint32_t i = 0; i < count; i++) {
        int idx = stat_map_index((sai_port_stat_t)ids[i]);
        if (idx >= 0 && row)
            values[i] = row->val[idx];
        else
            values[i] = 0;
    }
    pthread_mutex_unlock(&g_cache.lock);

    return SAI_STATUS_SUCCESS;
}

/* get_port_stats_ext: passthrough — the ext path (drop counters)
 * works for flex ports and must not be broken. */
static sai_status_t shim_get_port_stats_ext(
    sai_object_id_t  port_id,
    uint32_t         count,
    const uint32_t  *ids,
    int              mode,
    uint64_t        *values)
{
    return g_real_get_port_stats_ext(port_id, count, ids, mode, values);
}

/* ---- mprotect helper for patching read-only function pointer tables ---- */

/* Write a void* value to *dst, temporarily making the page writable if needed.
 * libsai.so maps its API struct tables with r-- protection; a plain write
 * causes SIGSEGV (fault code 7 = write to non-writable page).  We use
 * mprotect to add PROT_WRITE for the one page containing *dst, write, then
 * restore to PROT_READ. */
static int patch_fnptr(void **dst, void *val)
{
    long pgsz  = sysconf(_SC_PAGESIZE);
    uintptr_t addr = (uintptr_t)dst;
    uintptr_t page = addr & ~(uintptr_t)(pgsz - 1);

    if (mprotect((void *)page, (size_t)pgsz, PROT_READ | PROT_WRITE) != 0) {
        syslog(LOG_ERR, "shim: mprotect(PROT_READ|PROT_WRITE) failed at %p: %m",
               (void *)page);
        return -1;
    }
    *dst = val;
    /* Restore read-only; ignore failure (mapping may span multiple perms). */
    mprotect((void *)page, (size_t)pgsz, PROT_READ);
    return 0;
}

/* ---- sai_api_query intercept ---- */

/* Called once by syncd at startup, and again after each breakout change. */
sai_status_t sai_api_query(sai_api_t api, void **api_method_table)
{
    static sai_api_query_fn real_query = NULL;
    if (!real_query)
        real_query = (sai_api_query_fn)dlsym(RTLD_NEXT, "sai_api_query");
    if (!real_query) return -1;  /* SAI_STATUS_FAILURE */

    sai_status_t st = real_query(api, api_method_table);
    if (st != SAI_STATUS_SUCCESS || api != SAI_API_PORT)
        return st;

    sai_port_api_t *port_api = (sai_port_api_t *)*api_method_table;

    /* Save real function pointers only if they do not already point to our
     * shim — on the second and subsequent sai_api_query calls (breakout
     * reconfiguration) the struct may already be patched.  If we blindly
     * copy a shim pointer into g_real_*, the shim will recurse into itself
     * and stack-overflow. */
    if (port_api->get_port_stats != shim_get_port_stats)
        g_real_get_port_stats     = port_api->get_port_stats;
    if (port_api->get_port_stats_ext != shim_get_port_stats_ext)
        g_real_get_port_stats_ext = port_api->get_port_stats_ext;
    if (port_api->get_port_attribute != NULL &&
        port_api->get_port_attribute != (sai_get_port_attribute_fn)shim_get_port_stats)
        g_real_get_port_attr      = port_api->get_port_attribute;

    /* Replace with shim functions.  The struct may live in a read-only page
     * (libsai.so r-- mapping); use patch_fnptr to temporarily grant write
     * permission via mprotect before modifying. */
    patch_fnptr((void **)&port_api->get_port_stats,     shim_get_port_stats);
    patch_fnptr((void **)&port_api->get_port_stats_ext, shim_get_port_stats_ext);

    /* Invalidate OID cache — breakout may have changed port layout. */
    pthread_mutex_lock(&g_oids.lock);
    g_oids.n = 0;
    pthread_mutex_unlock(&g_oids.lock);

    /* Rebuild ps map (port layout may have changed) — reconnect to bcmcmd. */
    pthread_mutex_lock(&g_bcmfd_lock);
    if (g_bcmfd >= 0) { bcmcmd_close(g_bcmfd); g_bcmfd = -1; }
    g_ps_map_size = 0;
    pthread_mutex_unlock(&g_bcmfd_lock);

    if (!g_initialised) {
        /* One-time: parse BCM config file, initialise mutexes. */
        pthread_mutex_init(&g_oids.lock, NULL);
        pthread_mutex_init(&g_cache.lock, NULL);
        g_oids.n = 0;
        g_cache.n_rows = 0;

        const char *cfg = getenv(SHIM_BCM_CONFIG_ENV);
        if (cfg) parse_bcm_config(cfg);
        else syslog(LOG_WARNING, "shim: %s not set; lane→port map empty",
                    SHIM_BCM_CONFIG_ENV);

        g_initialised = 1;
        syslog(LOG_INFO, "shim: sai-stat-shim initialised (libsaibcm 14.3.x / BCM56960)");
    }

    return SAI_STATUS_SUCCESS;
}
