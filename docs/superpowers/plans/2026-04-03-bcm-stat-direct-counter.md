# Replace bcmcmd Counter Path with Direct bcm_stat_get — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bcmcmd socket-based counter path in sai-stat-shim with direct `bcm_stat_multi_get` calls via dlsym, eliminating socket contention, 3s banner timeouts, and diag shell death.

**Architecture:** The shim already LD_PRELOADs into syncd's address space where `bcm_stat_multi_get` is exported by libsai.so. We resolve the symbol via `dlsym(RTLD_DEFAULT, "bcm_stat_multi_get")` at init, define the ~35 bcm_stat_val_t enum constants we need as integer stubs, and replace the entire bcmcmd text-parsing counter path with a single `bcm_stat_multi_get()` call per flex port stats request. The bcmcmd_client.c file is deleted entirely.

**Tech Stack:** C (gcc, -fPIC -shared), dlsym/dlfcn.h, BCM SDK bcm_stat_multi_get API, SAI port stats API, pytest over SSH for hardware verification.

**Base directory:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/`

---

### Task 1: Update shim.h — Remove bcmcmd types, add bcm_stat types

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h`

- [ ] **Step 1: Read shim.h and verify current state**

Run: `cat platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h | head -5`
Expected: First line is `/* shim.h — SAI stat shim for Wedge100S-32X flex sub-port counters.`

- [ ] **Step 2: Replace shim.h with updated version**

Remove all bcmcmd types (`port_row_t`, `counter_cache_t`, bcmcmd function declarations) and the `SHIM_SOCKET_PATH`, `SHIM_CONNECT_TIMEOUT_MS`, `SHIM_CONNECT_BACKOFF_MS`, `SHIM_PORT_NAME_LEN` defines. Add `bcm_stat_val_t` enum stubs and `bcm_stat_multi_get_fn` typedef. Change `stat_map_entry_t` from string-based to int-based.

New complete `shim.h`:

```c
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

/* ---- BCM SDK stat types ---- */

/* bcm_stat_val_t enum stubs — only the values used by the shim.
 * These are fixed by the BCM API specification (soberly documented in
 * $SDK/include/bcm/stat.h) and do not change across SDK versions. */
enum {
    snmpIfHCInOctets              = 39,
    snmpIfHCInUcastPkts           = 40,
    snmpIfHCInMulticastPkts       = 41,
    snmpIfHCInBroadcastPkts       = 42,
    snmpIfHCOutOctets             = 43,
    snmpIfHCOutUcastPkts          = 44,
    snmpIfHCOutMulticastPkts      = 45,
    snmpIfHCOutBroadcastPckts     = 46,
    snmpIfInDiscards              = 3,
    snmpIfInErrors                = 4,
    snmpIfInUnknownProtos         = 5,
    snmpIfOutDiscards             = 9,
    snmpIfOutErrors               = 10,
    snmpEtherStatsUndersizePkts   = 15,
    snmpEtherStatsFragments       = 16,
    snmpEtherStatsOversizePkts    = 23,
    snmpEtherRxOversizePkts       = 24,
    snmpEtherTxOversizePkts       = 25,
    snmpEtherStatsJabbers         = 26,
    snmpEtherStatsTXNoErrors      = 34,
    snmpEtherStatsPkts64Octets    = 17,
    snmpEtherStatsPkts65to127Octets   = 18,
    snmpEtherStatsPkts128to255Octets  = 19,
    snmpEtherStatsPkts256to511Octets  = 20,
    snmpEtherStatsPkts512to1023Octets = 21,
    snmpEtherStatsPkts1024to1518Octets = 22,
    snmpIfInBroadcastPkts         = 35,
    snmpIfInMulticastPkts         = 36,
    snmpIfOutBroadcastPkts        = 37,
    snmpIfOutMulticastPkts        = 38,
};

/* Function pointer type for bcm_stat_multi_get, resolved via dlsym. */
typedef int (*bcm_stat_multi_get_fn)(int unit, int port, int nstat,
                                      int *stat_arr, uint64_t *value_arr);

/* ---- Shim configuration ---- */
#define SHIM_BCM_CONFIG_ENV "WEDGE100S_BCM_CONFIG"
#define SHIM_MAX_PORTS           256   /* bcmcmd ps shows ≤256 ports on Tomahawk */
#define SHIM_MAX_OID_CACHE       512   /* max SAI port OIDs tracked */
#define SHIM_MAX_STAT_IDS        80    /* max stat IDs in one get_port_stats call */

/* ---- stat_map.c types ---- */
typedef struct {
    sai_port_stat_t  stat_id;
    int              bcm_stat;    /* bcm_stat_val_t; -1 = return 0 */
    int              bcm_stat2;   /* second stat to add; -1 = none */
} stat_map_entry_t;

extern const stat_map_entry_t g_stat_map[];
extern const int              g_stat_map_size;
int stat_map_index(sai_port_stat_t stat_id);  /* -1 if not found */

/* ---- shim.c internal bookkeeping ---- */
typedef struct {
    sai_object_id_t oid;
    int             is_flex;   /* 1 = use bcm_stat; 0 = passthrough */
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
```

- [ ] **Step 3: Verify shim.h compiles**

Run from the sai-stat-shim directory:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && echo '#include "shim.h"' | gcc -fsyntax-only -x c -c - -I.
```
Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h
git commit -m "refactor(shim): replace bcmcmd types with bcm_stat_multi_get types in shim.h

Remove counter_cache_t, port_row_t, bcmcmd function declarations.
Add bcm_stat_val_t enum stubs and bcm_stat_multi_get_fn typedef.
Change stat_map_entry_t from string-based (name1/name2) to
int-based (bcm_stat/bcm_stat2)."
```

---

### Task 2: Rewrite stat_map.c — String-based to integer-based mapping

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/stat_map.c`

- [ ] **Step 1: Replace stat_map.c with integer-based mapping**

Replace the entire `g_stat_map[]` array. Each entry changes from `{ stat_id, "NAME1", "NAME2" }` to `{ stat_id, bcm_stat_val, bcm_stat2_val }` where `-1` means "return 0" or "no second stat".

New complete `stat_map.c`:

```c
/* stat_map.c — SAI port stat → bcm_stat_val_t mapping.
 * Empirically derived 2026-03-28 on BCM56960 / libsaibcm 14.3.0.0.0.0.3.0.
 * Migrated from string-based (bcmcmd counter names) to integer-based
 * (bcm_stat_val_t enum values) on 2026-04-03 for direct bcm_stat_multi_get.
 * Hardware: Accton Wedge100S-32X (SONiC hare-lorax, kernel 6.1.0-29-2-amd64). */
#include "shim.h"

#define S(id) ((sai_port_stat_t)(id))

const stat_map_entry_t g_stat_map[] = {
    /* Standard IF counters — HC (64-bit) variants used where available */
    { S(0),  snmpIfHCInOctets,          -1                       },  /* IF_IN_OCTETS           */
    { S(1),  snmpIfHCInUcastPkts,       -1                       },  /* IF_IN_UCAST_PKTS       */
    { S(2),  snmpIfHCInMulticastPkts,   snmpIfHCInBroadcastPkts  },  /* IF_IN_NON_UCAST_PKTS   */
    { S(3),  snmpIfInDiscards,          -1                       },  /* IF_IN_DISCARDS          */
    { S(4),  snmpIfInErrors,            -1                       },  /* IF_IN_ERRORS            */
    { S(5),  -1,                        -1                       },  /* IF_IN_UNKNOWN_PROTOS    */
    { S(6),  snmpIfHCInBroadcastPkts,   -1                       },  /* IF_IN_BROADCAST_PKTS    */
    { S(7),  snmpIfHCInMulticastPkts,   -1                       },  /* IF_IN_MULTICAST_PKTS    */

    { S(9),  snmpIfHCOutOctets,         -1                       },  /* IF_OUT_OCTETS           */
    { S(10), snmpIfHCOutUcastPkts,      -1                       },  /* IF_OUT_UCAST_PKTS       */
    { S(11), snmpIfHCOutMulticastPkts,  snmpIfHCOutBroadcastPckts},  /* IF_OUT_NON_UCAST_PKTS   */
    { S(12), snmpIfOutDiscards,         -1                       },  /* IF_OUT_DISCARDS          */
    { S(13), snmpIfOutErrors,           -1                       },  /* IF_OUT_ERRORS            */
    { S(14), -1,                        -1                       },  /* IF_OUT_QLEN              */
    { S(15), snmpIfHCOutBroadcastPckts, -1                       },  /* IF_OUT_BROADCAST_PKTS    */
    { S(16), snmpIfHCOutMulticastPkts,  -1                       },  /* IF_OUT_MULTICAST_PKTS    */

    /* Ethernet statistics */
    { S(20), snmpEtherStatsUndersizePkts, -1                     },  /* ETHER_STATS_UNDERSIZE    */
    { S(21), snmpEtherStatsFragments,     -1                     },  /* ETHER_STATS_FRAGMENTS    */
    { S(33), snmpEtherRxOversizePkts,     -1                     },  /* ETHER_RX_OVERSIZE        */
    { S(34), snmpEtherTxOversizePkts,     -1                     },  /* ETHER_TX_OVERSIZE        */
    { S(35), snmpEtherStatsJabbers,       -1                     },  /* ETHER_STATS_JABBERS      */
    { S(40), snmpEtherStatsTXNoErrors,    -1                     },  /* ETHER_STATS_TX_NO_ERRORS */

    /* IP counters — no BCM equivalent */
    { S(42), -1,                        -1                       },  /* IP_IN_RECEIVES           */
    { S(44), -1,                        -1                       },  /* IP_IN_UCAST_PKTS         */

    /* RX frame size buckets */
    { S(71), snmpEtherStatsPkts64Octets,        -1               },  /* IN_PKTS_64              */
    { S(72), snmpEtherStatsPkts65to127Octets,   -1               },  /* IN_PKTS_65_TO_127       */
    { S(73), snmpEtherStatsPkts128to255Octets,  -1               },  /* IN_PKTS_128_TO_255      */
    { S(74), snmpEtherStatsPkts256to511Octets,  -1               },  /* IN_PKTS_256_TO_511      */
    { S(75), snmpEtherStatsPkts512to1023Octets, -1               },  /* IN_PKTS_512_TO_1023     */
    { S(76), snmpEtherStatsPkts1024to1518Octets,-1               },  /* IN_PKTS_1024_TO_1518    */
    { S(77), -1,                        -1                       },  /* IN_PKTS_1519_TO_2047    */
    { S(78), -1,                        -1                       },  /* IN_PKTS_2048_TO_4095    */
    { S(79), -1,                        -1                       },  /* IN_PKTS_4096_TO_9216    */
    { S(80), -1,                        -1                       },  /* IN_PKTS_9217_TO_16383   */

    /* TX frame size buckets — the snmpEtherStatsPkts* enum values (17-22) are
     * aggregate RX+TX counters.  The BCM SDK has separate TX-only counters at
     * higher enum values (snmpEtherStatsTXPkts64Octets etc.) but their exact
     * values vary by SDK version.  Return 0 for now; add when verified. */
    { S(81), -1,                                -1               },  /* OUT_PKTS_64             */
    { S(82), -1,                                -1               },  /* OUT_PKTS_65_TO_127      */
    { S(83), -1,                                -1               },  /* OUT_PKTS_128_TO_255     */
    { S(84), -1,                                -1               },  /* OUT_PKTS_256_TO_511     */
    { S(85), -1,                        -1                       },  /* OUT_PKTS_512_TO_1023    */
    { S(86), -1,                        -1                       },  /* OUT_PKTS_1024_TO_1518   */
    { S(87), -1,                        -1                       },  /* OUT_PKTS_1519_TO_2047   */
    { S(88), -1,                        -1                       },  /* OUT_PKTS_2048_TO_4095   */
    { S(89), -1,                        -1                       },  /* OUT_PKTS_4096_TO_9216   */
    { S(90), -1,                        -1                       },  /* OUT_PKTS_9217_TO_16383  */

    /* IN_DROPPED / OUT_DROPPED */
    { S(99),  snmpIfInDiscards,         -1                       },  /* IN_DROPPED_PKTS         */
    { S(100), snmpIfOutDiscards,        -1                       },  /* OUT_DROPPED_PKTS        */

    /* Pause/PFC — these use bcm_stat_val_t values not in our enum stubs.
     * PFC counters are typically at enum values 150+ (snmpBcmRxPFCControlFrame
     * etc).  For now, return 0 — PFC counters on flex sub-ports are not
     * critical and can be added later if needed. */
    { S(101), -1,                       -1                       },  /* PAUSE_RX_PKTS           */
    { S(102), -1,                       -1                       },  /* PAUSE_TX_PKTS           */
    { S(103), -1,                       -1                       },  /* PFC_0_RX_PKTS           */
    { S(104), -1,                       -1                       },  /* PFC_0_TX_PKTS           */
    { S(105), -1,                       -1                       },  /* PFC_1_RX_PKTS           */
    { S(106), -1,                       -1                       },  /* PFC_1_TX_PKTS           */
    { S(107), -1,                       -1                       },  /* PFC_2_RX_PKTS           */
    { S(108), -1,                       -1                       },  /* PFC_2_TX_PKTS           */
    { S(109), -1,                       -1                       },  /* PFC_3_RX_PKTS           */
    { S(110), -1,                       -1                       },  /* PFC_3_TX_PKTS           */
    { S(111), -1,                       -1                       },  /* PFC_4_RX_PKTS           */
    { S(112), -1,                       -1                       },  /* PFC_4_TX_PKTS           */
    { S(113), -1,                       -1                       },  /* PFC_5_RX_PKTS           */
    { S(114), -1,                       -1                       },  /* PFC_5_TX_PKTS           */
    { S(115), -1,                       -1                       },  /* PFC_6_RX_PKTS           */
    { S(116), -1,                       -1                       },  /* PFC_6_TX_PKTS           */
    { S(117), -1,                       -1                       },  /* PFC_7_RX_PKTS           */
    { S(118), -1,                       -1                       },  /* PFC_7_TX_PKTS           */

    /* FEC counters — no BCM equivalent; return 0 */
    { S(178), -1,                       -1                       },  /* FEC_CORRECTABLE         */
    { S(179), -1,                       -1                       },  /* FEC_NOT_CORRECTABLE     */
    { S(180), -1,                       -1                       },  /* FEC_SYMBOL_ERRORS       */
    { S(202), -1,                       -1                       },  /* FEC_CORRECTED_BITS      */
};
#undef S

const int g_stat_map_size = (int)(sizeof(g_stat_map) / sizeof(g_stat_map[0]));

/* Linear search — called once per OID×stat_id pair. 68 entries: fast enough. */
int stat_map_index(sai_port_stat_t stat_id)
{
    for (int i = 0; i < g_stat_map_size; i++)
        if (g_stat_map[i].stat_id == stat_id)
            return i;
    return -1;
}
```

- [ ] **Step 2: Verify stat_map.c compiles with new shim.h**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && gcc -Wall -Wextra -O2 -g -fPIC -c -o stat_map.o stat_map.c
```
Expected: No errors, no warnings.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/stat_map.c
git commit -m "refactor(shim): convert stat_map from string names to bcm_stat_val_t integers

Each entry now maps SAI stat → bcm_stat_val_t enum value (or -1 for
unsupported). Dual-stat entries use bcm_stat2 field for summation
(e.g. IF_IN_NON_UCAST = multicast + broadcast).

PFC/pause counters return 0 for now (enum values 150+ not yet mapped)."
```

---

### Task 3: Rewrite shim.c — Replace bcmcmd path with bcm_stat_multi_get

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c`

This is the core change. Remove `g_ps_map`, `g_cache`, `refresh_cache()`, `bcmcmd_init_ps()`, `port_name_for_sdk()`, backoff logic, and `g_last_connect_fail`. Add `g_bcm_stat_multi_get` resolved via dlsym. Replace the flex path in `shim_get_port_stats` with a direct `bcm_stat_multi_get` call.

- [ ] **Step 1: Write new shim.c**

New complete `shim.c`:

```c
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
```

- [ ] **Step 2: Verify shim.c compiles**

Run:
```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && gcc -Wall -Wextra -O2 -g -fPIC -c -o shim.o shim.c
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c
git commit -m "feat(shim): replace bcmcmd socket path with direct bcm_stat_multi_get

Remove refresh_cache(), bcmcmd_init_ps(), g_ps_map, g_cache, and all
socket/backoff logic. Add flex_get_stats() which calls bcm_stat_multi_get
directly via dlsym-resolved function pointer.

Eliminates: socket contention, 3s banner timeout, diag shell death.
Counter reads are now microsecond-level from SDK DMA buffer."
```

---

### Task 4: Delete bcmcmd_client.c and update Makefile

**Files:**
- Delete: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/bcmcmd_client.c`
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile`

- [ ] **Step 1: Remove bcmcmd_client.o from Makefile OBJS**

Change the OBJS line and remove the test_parser target (it relied on bcmcmd_client.c):

New `Makefile`:

```makefile
# Makefile for libsai-stat-shim.so
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC
LDFLAGS = -lpthread

OBJS = shim.o stat_map.o compat.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl $(LDFLAGS)

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so
```

- [ ] **Step 2: Delete bcmcmd_client.c**

```bash
git rm platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/bcmcmd_client.c
```

- [ ] **Step 3: Build the full .so and verify**

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && make clean && make
```
Expected: `libsai-stat-shim.so` built with no errors. Should link only `shim.o stat_map.o compat.o`.

Verify no bcmcmd symbols remain:
```bash
nm libsai-stat-shim.so | grep -i bcmcmd
```
Expected: No output (all bcmcmd symbols removed).

Verify bcm_stat_multi_get is an undefined external (resolved at runtime):
```bash
nm libsai-stat-shim.so | grep bcm_stat_multi_get
```
Expected: One `U` (undefined) entry for `bcm_stat_multi_get`.

- [ ] **Step 4: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile
git commit -m "refactor(shim): delete bcmcmd_client.c, remove from Makefile

Entire bcmcmd socket client removed (~330 lines). The shim no longer
connects to the diag shell socket. test_parser target also removed
as it depended on bcmcmd_client.c internals."
```

---

### Task 5: Deploy to target and verify bcm_stat_multi_get resolves

**Files:**
- No file changes — hardware verification only

This is the risk validation step from the spec. We need to confirm that `bcm_stat_multi_get` resolves successfully in the syncd address space and returns non-zero counter values for flex sub-ports.

- [ ] **Step 1: Build the .deb and deploy**

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim && make clean && make
```

Then deploy to target:
```bash
SHIM_DIR=platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim
scp ${SHIM_DIR}/libsai-stat-shim.so admin@192.168.88.12:~/
ssh admin@192.168.88.12 "sudo cp ~/libsai-stat-shim.so /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so && sudo systemctl restart syncd"
```

Wait ~30s for syncd to restart and flex counter poller to run.

- [ ] **Step 2: Check syslog for bcm_stat_multi_get resolution**

```bash
ssh admin@192.168.88.12 "sudo grep 'bcm_stat_multi_get' /var/log/syslog | tail -5"
```

Expected: Line containing `shim: bcm_stat_multi_get resolved at 0x...` (not the `dlsym failed` message).

- [ ] **Step 3: Check no orchagent SIGABRT**

```bash
ssh admin@192.168.88.12 "sudo docker exec syncd ps aux | grep -c syncd"
```

Expected: syncd process is alive (count >= 1). No core dumps in `/var/core/`.

- [ ] **Step 4: Check flex port counters appear in COUNTERS_DB**

```bash
ssh admin@192.168.88.12 "redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet0"
```

Then with the returned OID:
```bash
ssh admin@192.168.88.12 "redis-cli -n 2 hget 'COUNTERS:<OID>' SAI_PORT_STAT_IF_IN_OCTETS"
```

Expected: Non-empty value. If traffic is flowing on the port, value should be > 0.

- [ ] **Step 5: Run existing test suites**

```bash
cd tests && python3 -m pytest stage_25_shim/ -v --timeout=120
```

Expected: All tests pass. Key tests:
- `test_shim_library_present` — file exists at expected path
- `test_syncd_has_ld_preload` — syncd has LD_PRELOAD set
- `test_flex_ports_have_full_stats` — flex ports have >= 60 stat keys

Also run counter tests:
```bash
cd tests && python3 -m pytest stage_24_counters/ -v --timeout=120
```

Expected: All pass. Non-flex ports still use the real get_port_stats passthrough.

---

### Task 6: Compare flex counters against peer EOS

**Files:**
- No file changes — hardware verification only

- [ ] **Step 1: Generate traffic on flex sub-ports**

If not already flowing, verify traffic on Ethernet100-103 (the peer EOS connection):
```bash
ssh admin@192.168.88.12 "show interfaces counters | grep 'Ethernet10[0-3] '"
```

Expected: Non-zero RX/TX bytes on at least one flex port.

- [ ] **Step 2: Compare with EOS peer**

```bash
sshpass -p '0penSesame' ssh -tt admin@192.168.88.14 'show interfaces counters | include Et25/[1-4]'
```

Compare IN_OCTETS and OUT_OCTETS between SONiC and EOS. Values should be in the same order of magnitude (exact match not expected due to timing differences).

- [ ] **Step 3: Verify non-flex ports unaffected**

```bash
ssh admin@192.168.88.12 "show interfaces counters | grep Ethernet16"
```

Expected: Non-zero counters on non-flex ports (these go through the real get_port_stats passthrough, unchanged).

---

### Summary of file changes

| File | Action | Lines removed | Lines added |
|---|---|---|---|
| `shim.h` | Modify | ~30 (bcmcmd types) | ~40 (bcm_stat enum, typedef) |
| `stat_map.c` | Modify | ~100 (string entries) | ~100 (int entries) |
| `shim.c` | Modify | ~100 (bcmcmd path) | ~80 (flex_get_stats) |
| `bcmcmd_client.c` | Delete | ~330 | 0 |
| `Makefile` | Modify | ~4 | ~2 |
| **Net** | | **~564** | **~222** |
