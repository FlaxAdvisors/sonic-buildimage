/* shim.c — LD_PRELOAD fault masker for sai_api_query(SAI_API_PORT).
 *
 * Single purpose: prevent FlexCounter from logging errors and dropping
 * flex port keys from COUNTERS_DB.
 *
 * If real get_port_stats returns SUCCESS → passthrough (no overhead).
 * If real get_port_stats returns non-SUCCESS → memset zeros, return SUCCESS.
 *
 * Does NOT touch any other function pointer in sai_port_api_t.
 * Does NOT do bcmcmd, caching, or classification.
 * The flex counter daemon handles real counter values separately.
 */
#define _GNU_SOURCE
#include "shim.h"

#include <string.h>
#include <dlfcn.h>
#include <syslog.h>

/* ---- globals ---- */

static sai_get_port_stats_fn g_real_get_port_stats = NULL;

/* Buffer for our copy of the port API struct.
 * Real SAI sai_port_api_t has 35 function pointer fields (280 bytes).
 * We copy the entire thing to be layout-agnostic. */
#define PORT_API_STRUCT_SIZE 512
static char g_shim_port_api_buf[PORT_API_STRUCT_SIZE]
    __attribute__((aligned(8)));

/* ---- fault masker ---- */

static sai_status_t shim_get_port_stats(
    sai_object_id_t oid, uint32_t count,
    const uint32_t *ids, uint64_t *values)
{
    sai_status_t st = g_real_get_port_stats(oid, count, ids, values);
    if (st == SAI_STATUS_SUCCESS)
        return st;
    memset(values, 0, count * sizeof(uint64_t));
    return SAI_STATUS_SUCCESS;
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

    sai_port_api_t *real_api = (sai_port_api_t *)*api_method_table;

    g_real_get_port_stats = real_api->get_port_stats;

    /* Copy the full 35-pointer struct, then patch our copy. */
    memcpy(g_shim_port_api_buf, real_api, 35 * sizeof(void *));
    sai_port_api_t *shim_api = (sai_port_api_t *)g_shim_port_api_buf;
    shim_api->get_port_stats = shim_get_port_stats;

    *api_method_table = (void *)shim_api;

    syslog(LOG_INFO, "shim: fault masker active (get_port_stats only)");
    return SAI_STATUS_SUCCESS;
}
