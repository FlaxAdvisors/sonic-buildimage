/* shim.h — SAI fault masker for Wedge100S-32X flex sub-port counters.
 * Masker-only: returns zeros+SUCCESS when real get_port_stats fails.
 * No bcmcmd, no cache, no lane mapping. */
#pragma once

#include <stdint.h>

/* ---- SAI type stubs (avoids build-time dependency on libsaibcm-dev) ---- */
typedef uint64_t sai_object_id_t;
typedef int32_t  sai_status_t;
typedef int32_t  sai_api_t;

#define SAI_STATUS_SUCCESS ((sai_status_t)0)
#define SAI_API_PORT       ((sai_api_t)2)

/* SAI port API function pointer types (subset needed by masker). */
typedef sai_status_t (*sai_get_port_stats_fn)(
    sai_object_id_t port_id, uint32_t count,
    const uint32_t *ids, uint64_t *values);

typedef sai_status_t (*sai_get_port_stats_ext_fn)(
    sai_object_id_t port_id, uint32_t count,
    const uint32_t *ids, int mode, uint64_t *values);

/* sai_port_api_t — only fields we touch.
 * Offsets verified against libsaibcm 14.3.0.0.0.0.3.0. */
typedef struct {
    void *create_port;           /* [0] */
    void *remove_port;           /* [1] */
    void *set_port_attribute;    /* [2] */
    void *get_port_attribute;    /* [3] */
    sai_get_port_stats_fn     get_port_stats;      /* [4] */
    sai_get_port_stats_ext_fn get_port_stats_ext;   /* [5] */
    void *clear_port_stats;      /* [6] */
} sai_port_api_t;

typedef sai_status_t (*sai_api_query_fn)(sai_api_t api, void **api_method_table);
