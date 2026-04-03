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
 *   via g_lane_map[]; fetch counters via bcm_stat_multi_get().
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

/* BCM SDK direct counter function, resolved via dlsym at init. */
static bcm_stat_multi_get_fn g_bcm_stat_multi_get = NULL;

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
        int sdk_port, phys_lane, speed;
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

/* ---- SDK port lookup helper ---- */

static int sdk_port_for_lane(uint32_t lane)
{
    for (int i = 0; i < g_lane_map_size; i++)
        if (g_lane_map[i].physical_lane == lane)
            return g_lane_map[i].sdk_port;
    return -1;
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

/* ---- Flex port counter read via bcm_stat_multi_get ---- */

/* Fetch counters for a flex sub-port directly from the BCM SDK.
 * Builds a bcm_stat_val_t array from the requested SAI stat IDs,
 * calls bcm_stat_multi_get once, then sums dual-stat entries. */
static sai_status_t flex_get_stats(int sdk_port, uint32_t count,
                                    const uint32_t *ids, uint64_t *values)
{
    if (!g_bcm_stat_multi_get) {
        memset(values, 0, count * sizeof(uint64_t));
        return SAI_STATUS_SUCCESS;
    }

    /* Build parallel arrays: one bcm_stat per requested SAI stat,
     * plus a second bcm_stat for dual-stat entries. We batch all
     * primary stats in one bcm_stat_multi_get call, then fetch
     * any secondary stats in a second call. */
    int    primary_stats[SHIM_MAX_STAT_IDS];
    int    primary_map[SHIM_MAX_STAT_IDS];   /* index into values[] */
    int    n_primary = 0;

    int    secondary_stats[SHIM_MAX_STAT_IDS];
    int    secondary_map[SHIM_MAX_STAT_IDS]; /* index into values[] */
    int    n_secondary = 0;

    for (uint32_t i = 0; i < count && i < SHIM_MAX_STAT_IDS; i++) {
        int idx = stat_map_index((sai_port_stat_t)ids[i]);
        if (idx < 0 || g_stat_map[idx].bcm_stat < 0) {
            values[i] = 0;
            continue;
        }
        primary_stats[n_primary] = g_stat_map[idx].bcm_stat;
        primary_map[n_primary] = (int)i;
        n_primary++;

        if (g_stat_map[idx].bcm_stat2 >= 0) {
            secondary_stats[n_secondary] = g_stat_map[idx].bcm_stat2;
            secondary_map[n_secondary] = (int)i;
            n_secondary++;
        }
    }

    /* Zero all values first (covers unmapped stats). */
    memset(values, 0, count * sizeof(uint64_t));

    /* Primary batch. */
    if (n_primary > 0) {
        uint64_t primary_vals[SHIM_MAX_STAT_IDS];
        int rc = g_bcm_stat_multi_get(0, sdk_port, n_primary,
                                       primary_stats, primary_vals);
        if (rc != 0) {
            static int logged = 0;
            if (!logged) {
                syslog(LOG_WARNING, "shim: bcm_stat_multi_get(port=%d) rc=%d",
                       sdk_port, rc);
                logged = 1;
            }
            return SAI_STATUS_SUCCESS;  /* zeros already set */
        }
        for (int i = 0; i < n_primary; i++)
            values[primary_map[i]] = primary_vals[i];
    }

    /* Secondary batch (dual-stat sums like non-ucast = mcast + bcast). */
    if (n_secondary > 0) {
        uint64_t secondary_vals[SHIM_MAX_STAT_IDS];
        int rc = g_bcm_stat_multi_get(0, sdk_port, n_secondary,
                                       secondary_stats, secondary_vals);
        if (rc != 0)
            return SAI_STATUS_SUCCESS;  /* primary values still valid */
        for (int i = 0; i < n_secondary; i++)
            values[secondary_map[i]] += secondary_vals[i];
    }

    return SAI_STATUS_SUCCESS;
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
        return g_real_get_port_stats(port_id, count, ids, values);
    }

    if (!entry) {
        sai_status_t st = g_real_get_port_stats(port_id, count, ids, values);
        if (st == SAI_STATUS_SUCCESS) {
            pthread_mutex_lock(&g_oids.lock);
            oid_insert(port_id, 0, -1);
            pthread_mutex_unlock(&g_oids.lock);
            return st;
        }
        int sdk_port = resolve_sdk_port(port_id);
        if (sdk_port < 0) {
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

    /* Flex path: direct BCM SDK call. */
    return flex_get_stats(entry->sdk_port, count, ids, values);
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
    mprotect((void *)page, (size_t)pgsz, PROT_READ);
    return 0;
}

/* ---- sai_api_query intercept ---- */

sai_status_t sai_api_query(sai_api_t api, void **api_method_table)
{
    static sai_api_query_fn real_query = NULL;
    if (!real_query)
        real_query = (sai_api_query_fn)dlsym(RTLD_NEXT, "sai_api_query");
    if (!real_query) return -1;

    sai_status_t st = real_query(api, api_method_table);
    if (st != SAI_STATUS_SUCCESS || api != SAI_API_PORT)
        return st;

    sai_port_api_t *port_api = (sai_port_api_t *)*api_method_table;

    if (port_api->get_port_stats != shim_get_port_stats)
        g_real_get_port_stats     = port_api->get_port_stats;
    if (port_api->get_port_stats_ext != shim_get_port_stats_ext)
        g_real_get_port_stats_ext = port_api->get_port_stats_ext;
    if (port_api->get_port_attribute != NULL)
        g_real_get_port_attr      = port_api->get_port_attribute;

    patch_fnptr((void **)&port_api->get_port_stats,     shim_get_port_stats);
    patch_fnptr((void **)&port_api->get_port_stats_ext, shim_get_port_stats_ext);

    /* Invalidate OID cache — breakout may have changed port layout. */
    pthread_mutex_lock(&g_oids.lock);
    g_oids.n = 0;
    pthread_mutex_unlock(&g_oids.lock);

    if (!g_initialised) {
        pthread_mutex_init(&g_oids.lock, NULL);
        g_oids.n = 0;

        const char *cfg = getenv(SHIM_BCM_CONFIG_ENV);
        if (cfg) parse_bcm_config(cfg);
        else syslog(LOG_WARNING, "shim: %s not set; lane→port map empty",
                    SHIM_BCM_CONFIG_ENV);

        /* Resolve bcm_stat_multi_get from libsai.so (same address space). */
        g_bcm_stat_multi_get = (bcm_stat_multi_get_fn)dlsym(
            RTLD_DEFAULT, "bcm_stat_multi_get");
        if (!g_bcm_stat_multi_get)
            syslog(LOG_ERR, "shim: dlsym(bcm_stat_multi_get) failed: %s — "
                   "flex counters will return zeros", dlerror());
        else
            syslog(LOG_INFO, "shim: bcm_stat_multi_get resolved at %p",
                   (void *)g_bcm_stat_multi_get);

        g_initialised = 1;
        syslog(LOG_INFO, "shim: sai-stat-shim initialised (direct bcm_stat path)");
    }

    return SAI_STATUS_SUCCESS;
}
