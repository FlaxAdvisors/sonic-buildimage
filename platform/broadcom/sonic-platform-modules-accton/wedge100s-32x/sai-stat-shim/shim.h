/* shim.h — SAI stat shim for Wedge100S-32X flex sub-port counters.
 * All components include this header. */
#pragma once

#include <stdint.h>
#include <pthread.h>
#include <time.h>

/* ---- SAI type stubs (avoids build-time dependency on libsaibcm-dev) ---- */
/* These values are fixed by the OCP SAI specification and will not change. */
typedef uint64_t sai_object_id_t;
typedef int32_t  sai_status_t;
typedef uint32_t sai_port_stat_t;
typedef uint32_t sai_attr_id_t;
typedef int32_t  sai_api_t;

#define SAI_STATUS_SUCCESS     ((sai_status_t)0)
#define SAI_API_PORT           ((sai_api_t)2)       /* from sai/sai.h: SAI_API_PORT=2 */
#define SAI_PORT_ATTR_HW_LANE_LIST ((sai_attr_id_t)30) /* from sai/saiport.h: 30th entry (0x1e) */

/* SAI attribute value union — only the u32list field is used by the shim. */
typedef struct { uint32_t count; uint32_t *list; } sai_u32_list_t;
typedef union {
    uint8_t       u8;
    int8_t        s8;
    uint16_t      u16;
    int16_t       s16;
    uint32_t      u32;
    int32_t       s32;
    uint64_t      u64;
    int64_t       s64;
    uint8_t       u8list[1024];  /* pad to match real SAI union size */
    sai_u32_list_t u32list;
} sai_attribute_value_t;

typedef struct {
    sai_attr_id_t         id;
    sai_attribute_value_t value;
} sai_attribute_t;

/* SAI port API function pointer types (subset needed by shim). */
typedef sai_status_t (*sai_get_port_stats_fn)(
    sai_object_id_t   port_id,
    uint32_t          number_of_counters,
    const uint32_t   *counter_ids,
    uint64_t         *counters);

typedef sai_status_t (*sai_get_port_stats_ext_fn)(
    sai_object_id_t   port_id,
    uint32_t          number_of_counters,
    const uint32_t   *counter_ids,
    int               mode,
    uint64_t         *counters);

typedef sai_status_t (*sai_get_port_attribute_fn)(
    sai_object_id_t    port_id,
    uint32_t           attr_count,
    sai_attribute_t   *attr_list);

/* sai_port_api_t — we only list the fields we touch.
 * Real struct has more fields before and after; the offsets below are
 * verified against libsaibcm 14.3.0.0.0.0.3.0 on this platform.
 * If the .so is upgraded, recheck with:
 *   readelf -s /usr/lib/libsai.so.1.0 | grep sai_port_api
 *   gdb -ex "ptype sai_port_api_t" /usr/bin/syncd
 */
typedef struct {
    void *create_port;           /* [0]  */
    void *remove_port;           /* [1]  */
    void *set_port_attribute;    /* [2]  */
    sai_get_port_attribute_fn get_port_attribute;  /* [3] */
    sai_get_port_stats_fn     get_port_stats;      /* [4] */
    sai_get_port_stats_ext_fn get_port_stats_ext;  /* [5] */
    void *clear_port_stats;      /* [6]  */
    /* Additional fields exist but are not accessed by the shim. */
} sai_port_api_t;

typedef sai_status_t (*sai_api_query_fn)(sai_api_t api, void **api_method_table);

/* ---- Shim configuration ---- */
#define SHIM_SOCKET_PATH    "/var/run/sswsyncd/sswsyncd.socket"
#define SHIM_BCM_CONFIG_ENV "WEDGE100S_BCM_CONFIG"
#define SHIM_CONNECT_TIMEOUT_MS  50
#define SHIM_MAX_PORTS           256   /* bcmcmd ps shows ≤256 ports on Tomahawk */
#define SHIM_MAX_OID_CACHE       512   /* max SAI port OIDs tracked */
#define SHIM_MAX_STAT_IDS        80    /* max stat IDs in one get_port_stats call */
#define SHIM_PORT_NAME_LEN       16    /* "xe86\0" fits in 6; 16 is generous */

/* ---- stat_map.c types ---- */
typedef struct {
    sai_port_stat_t  stat_id;
    const char      *name1;  /* bcmcmd counter name; NULL → return 0 */
    const char      *name2;  /* second counter to add (for non-ucast sums); NULL if single */
} stat_map_entry_t;

extern const stat_map_entry_t g_stat_map[];
extern const int              g_stat_map_size;
int stat_map_index(sai_port_stat_t stat_id);  /* -1 if not found */

/* ---- bcmcmd_client.c types ---- */

/* One row in the port counter cache: port_name + value per stat_map index. */
typedef struct {
    char     port_name[SHIM_PORT_NAME_LEN];  /* "xe86", "ce0" */
    int      sdk_port;                        /* numeric SDK port from ps */
    uint64_t val[SHIM_MAX_STAT_IDS];         /* indexed by g_stat_map index */
    /* Raw named counter lookup for name2 sums — flat table of (name, value) pairs. */
    int      n_raw;
    struct { char name[24]; uint64_t value; } raw[64];
} port_row_t;

typedef struct {
    port_row_t      rows[SHIM_MAX_PORTS];
    int             n_rows;
    struct timespec fetched_at;    /* CLOCK_MONOTONIC */
    pthread_mutex_t lock;
} counter_cache_t;

int  bcmcmd_connect(const char *path, int timeout_ms);   /* fd or -1 */
void bcmcmd_close(int fd);
/* Runs 'ps' → fills sdk_ports[]/port_names[] up to max entries.
 * Returns count of ports parsed, or -1 on error. */
int  bcmcmd_ps(int fd, int *sdk_ports, char port_names[][SHIM_PORT_NAME_LEN], int max);
/* Runs 'show counters' → fills/refreshes cache rows.
 * Caller holds no lock (this function acquires internally).
 * Returns 0 on success, -1 on error. */
int  bcmcmd_fetch_counters(int fd, counter_cache_t *cache);

/* ---- shim.c internal bookkeeping ---- */
typedef struct {
    sai_object_id_t oid;
    int             is_flex;   /* 1 = use bcmcmd; 0 = passthrough */
    int             sdk_port;  /* set for flex ports */
    int             valid;
} oid_entry_t;

typedef struct {
    oid_entry_t     e[SHIM_MAX_OID_CACHE];
    int             n;
    pthread_mutex_t lock;
} oid_cache_t;

/* lane→SDK port mapping built from BCM config portmap lines. */
typedef struct {
    uint32_t physical_lane;
    int      sdk_port;
} lane_map_entry_t;

#define SHIM_MAX_LANE_MAP 512
extern lane_map_entry_t g_lane_map[];
extern int              g_lane_map_size;
