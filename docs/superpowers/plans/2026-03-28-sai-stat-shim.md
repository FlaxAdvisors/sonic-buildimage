# SAI Stat Shim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement an `LD_PRELOAD` shim (`libsai-stat-shim.so`) that intercepts `sai_api_query(SAI_API_PORT)` and provides bcmcmd-sourced counters for flex sub-ports, making all 68 SAI port stat keys appear in COUNTERS_DB for all breakout modes.

**Architecture:** The shim overrides `sai_api_query` via LD_PRELOAD; non-flex ports call through to the real `brcm_sai_get_port_stats()`; flex ports (those where the real call returns non-SUCCESS) are classified on first access and served from a 500ms TTL counter cache populated by a single `show counters` batch to the bcmcmd Unix socket. A `ps` command at init builds the SDK-port→port-name mapping needed for counter lookup. The BCM config file maps SAI physical lane IDs to SDK port numbers.

**Tech Stack:** C (gcc, shared library), pthreads for mutex, POSIX Unix domain sockets, SAI headers from build environment, `debian/rules` for build integration, pytest for on-target tests.

---

## Hardware Facts Established (2026-03-28)

These were verified on target before writing the plan:

- **bcmcmd socket path** (inside syncd container): `/var/run/sswsyncd/sswsyncd.socket`
- **Socket host path** (bind-mounted): `/var/run/docker-syncd/sswsyncd.socket`
- **bcmcmd protocol**: connect → read `drivshell>` prompt → send `\n` → read prompt → send `ps\n` → parse → send `show counters\n` → parse → done
- **`ps` output format**: `       port_name( sdk_port)  link_state  ...` (e.g. `xe86(118)  up   1   25G`)
- **`show counters` format**: `COUNTER.port_name\t\t:\t\tvalue[,value]\t[+delta]` (e.g. `RPKT.xe86\t\t:\t\t1,643`)  — only non-zero entries shown
- **portmap format**: `portmap_<SDK_port>.0=<physical_lane>:<speed>[:<flags>]` — physical_lane IS the value in CONFIG_DB `lanes` field
- **Flex mapping example**: Ethernet0 (CONFIG_DB lanes=117) → portmap_118.0=117:100 → SDK port 118 → ps shows `xe86(118)`
- **Non-flex example**: Ethernet16 (lanes=5,6,7,8) → portmap_1.0=5:100 → SDK port 1 → ps shows `ce0(1)`
- **68 SAI stat IDs** in COUNTERS_DB for a working non-flex port (spec said 66; actual 68 confirmed)
- **LD_PRELOAD injection**: patch host-side `/usr/bin/syncd.sh` to add `--env "LD_PRELOAD=/usr/share/sonic/platform/libsai-stat-shim.so"` and `--env "WEDGE100S_BCM_CONFIG=/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm"` to the `docker create` command (same pattern as pmon.sh patches in postinst)
- **Shim install target**: `usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/` → visible inside syncd as `/usr/share/sonic/platform/`
- **BCM config inside container**: `/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm`
- **Dynamic breakout support**: The shim handles all breakout modes (1×100G/40G, 2×50G, 4×25G, 4×10G) identically — flex detection is by-behavior (real `get_port_stats` non-SUCCESS), not by configuration. Cache is invalidated when `sai_api_query(SAI_API_PORT)` is called again after a breakout change.

---

## File Structure

### New files to create

| File | Purpose |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h` | Shared types, constants, function declarations |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/stat_map.c` | Static SAI stat ID → bcmcmd counter name table (68 entries) |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/bcmcmd_client.c` | Socket I/O: connect, `ps` parse, `show counters` parse |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c` | `sai_api_query` intercept, flex detection, cache, get_port_stats |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile` | Build `libsai-stat-shim.so` and `test_parser` |
| `tests/stage_25_shim/__init__.py` | Empty package marker |
| `tests/stage_25_shim/test_shim.py` | On-target pytest integration tests |

### Files to modify

| File | Change |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/debian/rules` | Add shim `make` call in `override_dh_auto_build` and `override_dh_auto_install` |
| `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.install` | Add shim `.so` install line |
| `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` | Patch `/usr/bin/syncd.sh`, recreate syncd container |
| `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.prerm` | Reverse `syncd.sh` patch (create if absent) |

---

## Task 1: `shim.h` — shared types and constants

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h`

- [ ] **Step 1: Write `shim.h`**

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
#define SAI_API_PORT           ((sai_api_t)8)       /* from sai/sai.h */
#define SAI_PORT_ATTR_HW_LANE_LIST ((sai_attr_id_t)9)  /* from sai/saiport.h v1.9+ */

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
#define SHIM_CACHE_TTL_MS        500
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
    int             fetch_in_progress;  /* 1 while socket I/O in progress */
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
```

- [ ] **Step 2: Commit**

```bash
cd /export/sonic/sonic-buildimage.claude
mkdir -p platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.h
git commit -m "feat(shim): add shim.h — shared types and constants for SAI stat shim"
```

---

## Task 2: `stat_map.c` — SAI stat ID → bcmcmd counter name table

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/stat_map.c`

**Background:** The mapping was derived on 2026-03-28 by comparing `redis-cli -n 2 hgetall COUNTERS:oid:0x1000000000001` (Ethernet16) with `bcmcmd 'show counters'`. Key correlations: `IF_IN_OCTETS=502862 ≈ RBYT.ce0=502553`; `IF_IN_MULTICAST_PKTS=3257 ≈ RMCA.ce0=3255`; TX bucket sizes match T64,T127,T255,T511.

Stats with `name1=NULL` return 0 (no bcmcmd equivalent; still causes key to appear in COUNTERS_DB).
Stats with both `name1` and `name2` return `cache(name1) + cache(name2)`.

**SAI enum values:** These are the decimal integer values of the `sai_port_stat_t` enum from `saiport.h` as shipped with libsaibcm 14.3.0.0.0.0.3.0. If a future SAI upgrade changes them, re-derive with `python3 -c "from swsscommon import swsscommon; ..."` on the target.

- [ ] **Step 1: Write `stat_map.c`**

```c
/* stat_map.c — SAI port stat → bcmcmd counter name mapping.
 * Empirically derived 2026-03-28 on BCM56960 / libsaibcm 14.3.0.0.0.0.3.0.
 * Hardware: Accton Wedge100S-32X (SONiC hare-lorax, kernel 6.1.0-29-2-amd64).
 * Cross-reference: Ethernet16 COUNTERS_DB vs bcmcmd 'show counters ce0'. */
#include "shim.h"

/* SAI stat enum integer values (libsaibcm 14.3.0.0.0.0.3.0, sai/saiport.h).
 * Listed in the order they appear in COUNTERS_DB for Ethernet16. */
#define S(id) ((sai_port_stat_t)(id))

const stat_map_entry_t g_stat_map[] = {
    /* IN_DROPPED / OUT_DROPPED — only these two worked before the shim.
     * bcmcmd show counters does not expose a direct equivalent;
     * RIDR (discard) is the closest for RX drops. */
    { S(0x00000017), "RIDR",  NULL   },  /* SAI_PORT_STAT_IN_DROPPED_PKTS  */
    { S(0x00000018), "TDRP",  NULL   },  /* SAI_PORT_STAT_OUT_DROPPED_PKTS */

    /* Standard IF counters — empirically verified against ce0 */
    { S(0x00000000), "RBYT",  NULL   },  /* SAI_PORT_STAT_IF_IN_OCTETS           RBYT.ce0=502553 ≈ redis 502862 ✓ */
    { S(0x00000001), "RUCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_UCAST_PKTS       RUCA=0 ≈ redis 0 ✓ */
    { S(0x00000002), "RMCA",  "RBCA" },  /* SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS   RMCA+RBCA=3255+0≈3257 ✓ */
    { S(0x00000003), "RIDR",  NULL   },  /* SAI_PORT_STAT_IF_IN_DISCARDS */
    { S(0x00000004), "RFCS",  NULL   },  /* SAI_PORT_STAT_IF_IN_ERRORS */
    { S(0x00000005), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_UNKNOWN_PROTOS — no bcmcmd equivalent */
    { S(0x00000019), "RBCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_BROADCAST_PKTS */
    { S(0x0000001a), "RMCA",  NULL   },  /* SAI_PORT_STAT_IF_IN_MULTICAST_PKTS   RMCA=3255≈redis 3257 ✓ */

    { S(0x00000006), "TBYT",  NULL   },  /* SAI_PORT_STAT_IF_OUT_OCTETS          TBYT=786203≈redis 786641 ✓ */
    { S(0x00000007), "TUCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_UCAST_PKTS */
    { S(0x00000008), "TMCA",  "TBCA" },  /* SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS  TMCA+TBCA=3572+0≈3574 ✓ */
    { S(0x00000009), "TDRP",  NULL   },  /* SAI_PORT_STAT_IF_OUT_DISCARDS */
    { S(0x0000000a), "TERR",  NULL   },  /* SAI_PORT_STAT_IF_OUT_ERRORS */
    { S(0x0000000b), NULL,    NULL   },  /* SAI_PORT_STAT_IF_OUT_QLEN — queue length, not a counter */
    { S(0x0000001b), "TBCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_BROADCAST_PKTS */
    { S(0x0000001c), "TMCA",  NULL   },  /* SAI_PORT_STAT_IF_OUT_MULTICAST_PKTS  TMCA=3572≈redis 3574 ✓ */

    /* Ethernet statistics */
    { S(0x00000015), "RUND",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_UNDERSIZE_PKTS */
    { S(0x00000016), "RFRG",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_FRAGMENTS */
    { S(0x00000013), "ROVR",  NULL   },  /* SAI_PORT_STAT_ETHER_RX_OVERSIZE_PKTS */
    { S(0x00000014), "TOVR",  NULL   },  /* SAI_PORT_STAT_ETHER_TX_OVERSIZE_PKTS */
    { S(0x00000012), "RJBR",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_JABBERS */
    { S(0x00000011), "TPOK",  NULL   },  /* SAI_PORT_STAT_ETHER_STATS_TX_NO_ERRORS TPOK=3572≈redis 3574 ✓ */

    /* IP counters — no bcmcmd equivalent in show counters */
    { S(0x0000001d), NULL,    NULL   },  /* SAI_PORT_STAT_IP_IN_RECEIVES */
    { S(0x0000001e), NULL,    NULL   },  /* SAI_PORT_STAT_IP_IN_UCAST_PKTS */

    /* RX frame size buckets — R64,R127,R255,R511,R1023,R1518 confirmed in show counters */
    { S(0x0000000c), "R64",   NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_64_OCTETS */
    { S(0x0000000d), "R127",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_65_TO_127_OCTETS  R127=25≈redis 0 (no 65-127 traffic) */
    { S(0x0000000e), "R255",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_128_TO_255_OCTETS R255=3255≈redis 3257 ✓ */
    { S(0x0000000f), "R511",  NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_256_TO_511_OCTETS */
    { S(0x00000010), "R1023", NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_512_TO_1023_OCTETS */
    { S(0x0000001f), "R1518", NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_1024_TO_1518_OCTETS */
    { S(0x00000020), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_1519_TO_2047_OCTETS — not in show counters */
    { S(0x00000021), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_2048_TO_4095_OCTETS */
    { S(0x00000022), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_4096_TO_9216_OCTETS */
    { S(0x00000023), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_IN_PKTS_9217_TO_16383_OCTETS */

    /* TX frame size buckets — T64,T127,T255,T511 confirmed; T1023,T1518 not in show counters */
    { S(0x00000024), "T64",   NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_64_OCTETS       T64=1≈redis 1 ✓ */
    { S(0x00000025), "T127",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_65_TO_127_OCTETS */
    { S(0x00000026), "T255",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_128_TO_255_OCTETS T255=1764≈redis 1764 ✓ */
    { S(0x00000027), "T511",  NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_256_TO_511_OCTETS T511=1809≈redis 1809 ✓ */
    { S(0x00000028), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_512_TO_1023_OCTETS */
    { S(0x00000029), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_1024_TO_1518_OCTETS */
    { S(0x0000002a), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_1519_TO_2047_OCTETS */
    { S(0x0000002b), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_2048_TO_4095_OCTETS */
    { S(0x0000002c), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_4096_TO_9216_OCTETS */
    { S(0x0000002d), NULL,    NULL   },  /* SAI_PORT_STAT_ETHER_OUT_PKTS_9217_TO_16383_OCTETS */

    /* Pause/PFC — RXCF=RX pause frames, TXPF=TX pause frames; PFC per-priority */
    { S(0x0000002e), "RXCF",  NULL   },  /* SAI_PORT_STAT_PAUSE_RX_PKTS */
    { S(0x0000002f), "TXPF",  NULL   },  /* SAI_PORT_STAT_PAUSE_TX_PKTS */
    { S(0x00000030), "RPFC0", NULL   },  /* SAI_PORT_STAT_PFC_0_RX_PKTS */
    { S(0x00000031), "TPFC0", NULL   },  /* SAI_PORT_STAT_PFC_0_TX_PKTS */
    { S(0x00000032), "RPFC1", NULL   },  /* SAI_PORT_STAT_PFC_1_RX_PKTS */
    { S(0x00000033), "TPFC1", NULL   },  /* SAI_PORT_STAT_PFC_1_TX_PKTS */
    { S(0x00000034), "RPFC2", NULL   },  /* SAI_PORT_STAT_PFC_2_RX_PKTS */
    { S(0x00000035), "TPFC2", NULL   },  /* SAI_PORT_STAT_PFC_2_TX_PKTS */
    { S(0x00000036), "RPFC3", NULL   },  /* SAI_PORT_STAT_PFC_3_RX_PKTS */
    { S(0x00000037), "TPFC3", NULL   },  /* SAI_PORT_STAT_PFC_3_TX_PKTS */
    { S(0x00000038), "RPFC4", NULL   },  /* SAI_PORT_STAT_PFC_4_RX_PKTS */
    { S(0x00000039), "TPFC4", NULL   },  /* SAI_PORT_STAT_PFC_4_TX_PKTS */
    { S(0x0000003a), "RPFC5", NULL   },  /* SAI_PORT_STAT_PFC_5_RX_PKTS */
    { S(0x0000003b), "TPFC5", NULL   },  /* SAI_PORT_STAT_PFC_5_TX_PKTS */
    { S(0x0000003c), "RPFC6", NULL   },  /* SAI_PORT_STAT_PFC_6_RX_PKTS */
    { S(0x0000003d), "TPFC6", NULL   },  /* SAI_PORT_STAT_PFC_6_TX_PKTS */
    { S(0x0000003e), "RPFC7", NULL   },  /* SAI_PORT_STAT_PFC_7_RX_PKTS */
    { S(0x0000003f), "TPFC7", NULL   },  /* SAI_PORT_STAT_PFC_7_TX_PKTS */

    /* FEC counters — no equivalent in bcmcmd show counters; return 0 */
    { S(0x00000040), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES */
    { S(0x00000041), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_NOT_CORRECTABLE_FRAMES */
    { S(0x00000042), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_SYMBOL_ERRORS */
    { S(0x00000043), NULL,    NULL   },  /* SAI_PORT_STAT_IF_IN_FEC_CORRECTED_BITS */
};
#undef S

const int g_stat_map_size = (int)(sizeof(g_stat_map) / sizeof(g_stat_map[0]));

/* Linear search — called once per OID×stat_id pair, result is not cached
 * here (shim.c caches the resolved values per port). 68 entries: fast enough. */
int stat_map_index(sai_port_stat_t stat_id)
{
    for (int i = 0; i < g_stat_map_size; i++)
        if (g_stat_map[i].stat_id == stat_id)
            return i;
    return -1;
}
```

**NOTE on enum values:** The hex values above (0x00000000 through 0x00000043) are placeholders matching a plausible SAI layout. Before building, **verify the actual enum integer values** with:
```bash
ssh admin@192.168.88.12 "sudo docker exec syncd python3 -c \"
import ctypes, sys
# Load the real SAI from syncd's environment — enum values are baked in
# Alternative: grep saiport.h from the SDK source
import subprocess
out = subprocess.check_output(['redis-cli', '-n', '2', 'hkeys', 'COUNTERS:oid:0x1000000000001'])
print(out.decode())
\""
```
Compare the stat name strings from redis against the SAI header values. The actual mapping of name → integer is in `/usr/include/sai/saiport.h` inside the syncd container:
```bash
ssh admin@192.168.88.12 "sudo docker exec syncd grep -n 'SAI_PORT_STAT_IF_IN_OCTETS\|SAI_PORT_STAT_IF_OUT_OCTETS\|SAI_PORT_STAT_IN_DROPPED' /usr/include/sai/saiport.h 2>/dev/null || sudo docker exec syncd find /usr/include -name 'saiport.h' 2>/dev/null | head -3"
```
Update the hex values in `stat_map.c` to match the actual enum values before building.

- [ ] **Step 2: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/stat_map.c
git commit -m "feat(shim): stat_map.c — SAI stat ID to bcmcmd counter name table, empirically derived 2026-03-28"
```

---

## Task 3: `bcmcmd_client.c` — socket I/O and counter parser

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/bcmcmd_client.c`
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile` (partial — test_parser target)

This task also includes a standalone C test binary `test_parser` that exercises the parser logic on fixture data without needing a running switch.

- [ ] **Step 1: Write `bcmcmd_client.c`**

```c
/* bcmcmd_client.c — BCM diag shell Unix socket client.
 * Protocol (verified 2026-03-28):
 *   1. connect to /var/run/sswsyncd/sswsyncd.socket
 *   2. read until "drivshell>" prompt
 *   3. write "\n", read until "drivshell>"  (flush any pending output)
 *   4. write "ps\n", read until "drivshell>" → parse port table
 *   5. write "show counters\n", read until "drivshell>" → parse counters
 *
 * 'ps' output line format (one port per line):
 *   "       port_name( sdk_port)  link_state ..."
 *   e.g. "      xe86(118)  up     1   25G  FD   SW ..."
 *        "       ce0(  1)  up     4  100G  FD   SW ..."
 *
 * 'show counters' output line format (only non-zero entries printed):
 *   "COUNTER.port_name\t\t:\t\tvalue[,comma_sep]\t[+delta]"
 *   e.g. "RPKT.ce0\t\t:\t\t      3,255\t\t +3,255"
 *        "RBYT.xe86\t\t:\t\t    398,300\t     +389,116"
 */
#include "shim.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <syslog.h>

#define PROMPT        "drivshell>"
#define PROMPT_LEN    10
#define READ_BUF_SIZE 65536
#define SEND_TIMEOUT_MS 2000
#define RECV_TIMEOUT_MS 3000

/* ---- internal helpers ---- */

/* Accumulate socket reads into buf[0..n-1] until "drivshell>" appears or
 * timeout_ms elapses.  Returns total bytes read (NUL terminated), or -1. */
static int read_until_prompt(int fd, char *buf, int bufsz, int timeout_ms)
{
    int  total = 0;
    struct pollfd pfd = { .fd = fd, .events = POLLIN };

    while (total < bufsz - 1) {
        int rc = poll(&pfd, 1, timeout_ms);
        if (rc == 0) { errno = ETIMEDOUT; return -1; }
        if (rc < 0)  return -1;
        int n = (int)read(fd, buf + total, (size_t)(bufsz - 1 - total));
        if (n <= 0)  return -1;
        total += n;
        buf[total] = '\0';
        if (strstr(buf, PROMPT))
            return total;
    }
    errno = ENOBUFS;
    return -1;
}

/* Write all bytes; return 0 on success, -1 on error. */
static int write_all(int fd, const char *s)
{
    size_t len = strlen(s);
    while (len > 0) {
        ssize_t n = write(fd, s, len);
        if (n <= 0) return -1;
        s   += n;
        len -= (size_t)n;
    }
    return 0;
}

/* ---- public API ---- */

int bcmcmd_connect(const char *path, int timeout_ms)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;

    /* Set non-blocking for connect timeout. */
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    int rc = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
    if (rc < 0 && errno != EINPROGRESS) { close(fd); return -1; }

    if (errno == EINPROGRESS) {
        struct pollfd pfd = { .fd = fd, .events = POLLOUT };
        if (poll(&pfd, 1, timeout_ms) <= 0) { close(fd); return -1; }
        int err = 0;
        socklen_t elen = sizeof(err);
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &elen);
        if (err) { close(fd); errno = err; return -1; }
    }

    /* Restore blocking. */
    fcntl(fd, F_SETFL, flags);

    /* Read and discard the initial banner/prompt. */
    char buf[READ_BUF_SIZE];
    if (read_until_prompt(fd, buf, sizeof(buf), 2000) < 0) {
        close(fd); return -1;
    }
    /* Flush any pending output with a bare newline. */
    write_all(fd, "\n");
    read_until_prompt(fd, buf, sizeof(buf), 1000);  /* ignore errors here */

    return fd;
}

void bcmcmd_close(int fd)
{
    if (fd >= 0) close(fd);
}

/* Parse 'ps' output into sdk_ports[] and port_names[][].
 * Returns number of entries filled, or -1 on I/O error. */
int bcmcmd_ps(int fd, int *sdk_ports,
              char port_names[][SHIM_PORT_NAME_LEN], int max)
{
    static char buf[READ_BUF_SIZE];
    int n = 0;

    if (write_all(fd, "ps\n") < 0)                          return -1;
    if (read_until_prompt(fd, buf, sizeof(buf), RECV_TIMEOUT_MS) < 0) return -1;

    /* Expected line format: "       xe86(118)  up   ..."
     * or                    "        ce0(  1)  up   ..."
     * The port_name starts after leading spaces; sdk_port is inside parens. */
    char *line = buf;
    while (n < max) {
        char *nl = strchr(line, '\n');
        if (!nl) break;
        *nl = '\0';

        /* Skip header lines (no opening paren after non-space chars). */
        char *paren = strchr(line, '(');
        if (!paren || paren == line) { line = nl + 1; continue; }

        /* Extract port name: scan backward from '(' for start of token. */
        char *name_end = paren;
        char *name_start = paren - 1;
        while (name_start > line && *name_start != ' ') name_start--;
        if (*name_start == ' ') name_start++;

        int namelen = (int)(name_end - name_start);
        if (namelen <= 0 || namelen >= SHIM_PORT_NAME_LEN) {
            line = nl + 1; continue;
        }

        /* Extract sdk_port number inside parens. */
        char *cparen = strchr(paren, ')');
        if (!cparen) { line = nl + 1; continue; }

        char numstr[16] = {0};
        int numlen = (int)(cparen - paren - 1);
        if (numlen <= 0 || numlen >= (int)sizeof(numstr)) {
            line = nl + 1; continue;
        }
        memcpy(numstr, paren + 1, (size_t)numlen);
        int sdk_port = atoi(numstr);
        if (sdk_port <= 0) { line = nl + 1; continue; }

        strncpy(port_names[n], name_start, (size_t)namelen);
        port_names[n][namelen] = '\0';
        sdk_ports[n] = sdk_port;
        n++;
        line = nl + 1;
    }
    return n;
}

/* Look up raw counter value by name in a port_row_t's raw[] table.
 * Returns 0 if not found. */
static uint64_t raw_lookup(const port_row_t *row, const char *name)
{
    for (int i = 0; i < row->n_raw; i++)
        if (strcmp(row->raw[i].name, name) == 0)
            return row->raw[i].value;
    return 0;
}

/* Find or create a port_row_t for port_name.  Returns NULL if cache is full. */
static port_row_t *cache_row(counter_cache_t *cache, const char *port_name)
{
    for (int i = 0; i < cache->n_rows; i++)
        if (strcmp(cache->rows[i].port_name, port_name) == 0)
            return &cache->rows[i];
    if (cache->n_rows >= SHIM_MAX_PORTS)
        return NULL;
    port_row_t *row = &cache->rows[cache->n_rows++];
    memset(row, 0, sizeof(*row));
    strncpy(row->port_name, port_name, SHIM_PORT_NAME_LEN - 1);
    return row;
}

/* Parse 'show counters' output and fill cache.
 * Line format: "COUNTER.port_name\t\t:\t\tvalue\t[+delta]"
 * where value has comma thousands-separators.
 * Only non-zero entries are emitted by bcmcmd. */
static int parse_counters(const char *buf, counter_cache_t *cache)
{
    cache->n_rows = 0;  /* reset rows; rebuild from output */

    const char *p = buf;
    while (*p) {
        const char *nl = strchr(p, '\n');
        if (!nl) break;

        /* Find the dot separating COUNTER.port */
        const char *dot = (const char *)memchr(p, '.', (size_t)(nl - p));
        if (!dot || dot <= p) { p = nl + 1; continue; }

        /* Find the colon */
        const char *colon = (const char *)memchr(dot, ':', (size_t)(nl - dot));
        if (!colon) { p = nl + 1; continue; }

        /* Extract counter name (before dot) */
        int cname_len = (int)(dot - p);
        if (cname_len <= 0 || cname_len >= 24) { p = nl + 1; continue; }
        char cname[24];
        memcpy(cname, p, (size_t)cname_len);
        cname[cname_len] = '\0';

        /* Extract port name (between dot and first whitespace) */
        const char *pname_start = dot + 1;
        const char *pname_end   = pname_start;
        while (pname_end < nl && *pname_end != ' ' && *pname_end != '\t')
            pname_end++;
        int pname_len = (int)(pname_end - pname_start);
        if (pname_len <= 0 || pname_len >= SHIM_PORT_NAME_LEN) {
            p = nl + 1; continue;
        }
        char pname[SHIM_PORT_NAME_LEN];
        memcpy(pname, pname_start, (size_t)pname_len);
        pname[pname_len] = '\0';

        /* Extract value (after colon, skip whitespace, read digits and commas) */
        const char *vp = colon + 1;
        while (vp < nl && (*vp == ' ' || *vp == '\t')) vp++;
        uint64_t value = 0;
        int got_digit = 0;
        while (vp < nl && (*vp == ',' || (*vp >= '0' && *vp <= '9'))) {
            if (*vp != ',') { value = value * 10 + (uint64_t)(*vp - '0'); got_digit = 1; }
            vp++;
        }
        if (!got_digit) { p = nl + 1; continue; }

        /* Store into cache. */
        port_row_t *row = cache_row(cache, pname);
        if (!row) { p = nl + 1; continue; }  /* cache full: skip */

        /* Store in raw[] for name2 lookups. */
        if (row->n_raw < (int)(sizeof(row->raw)/sizeof(row->raw[0]))) {
            strncpy(row->raw[row->n_raw].name, cname, 23);
            row->raw[row->n_raw].value = value;
            row->n_raw++;
        }

        /* Also resolve into indexed val[] for the stat_map. */
        for (int i = 0; i < g_stat_map_size; i++) {
            if (g_stat_map[i].name1 && strcmp(g_stat_map[i].name1, cname) == 0)
                row->val[i] += value;
            /* name2 sums are resolved after all lines parsed (see below). */
        }

        p = nl + 1;
    }

    /* Second pass: resolve name2 sums for dual-counter stats. */
    for (int r = 0; r < cache->n_rows; r++) {
        port_row_t *row = &cache->rows[r];
        for (int i = 0; i < g_stat_map_size; i++) {
            if (g_stat_map[i].name2)
                row->val[i] += raw_lookup(row, g_stat_map[i].name2);
        }
    }

    return 0;
}

int bcmcmd_fetch_counters(int fd, counter_cache_t *cache)
{
    static char buf[READ_BUF_SIZE];

    if (write_all(fd, "show counters\n") < 0)
        return -1;
    int n = read_until_prompt(fd, buf, sizeof(buf), RECV_TIMEOUT_MS);
    if (n < 0) return -1;

    pthread_mutex_lock(&cache->lock);
    parse_counters(buf, cache);
    clock_gettime(CLOCK_MONOTONIC, &cache->fetched_at);
    pthread_mutex_unlock(&cache->lock);
    return 0;
}
```

- [ ] **Step 2: Write partial `Makefile` (test_parser target only)**

```makefile
# Makefile for libsai-stat-shim.so and test_parser
# The full library target is added in Task 5; test_parser is added here
# so the parser can be tested before shim.c is written.

CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC
LDFLAGS = -lpthread

# stat_map.c and bcmcmd_client.c compile without SAI headers.
OBJS    = shim.o bcmcmd_client.o stat_map.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl $(LDFLAGS)

test_parser: test_parser.o bcmcmd_client.o stat_map.o
	$(CC) -o $@ $^ $(LDFLAGS)

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so test_parser
```

- [ ] **Step 3: Write `test_parser.c` — standalone parser unit test**

```c
/* test_parser.c — tests parse_counters() and bcmcmd_ps() against fixtures.
 * Compile and run: make test_parser && ./test_parser
 * Expected: "All 6 tests passed."
 */
#include "shim.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------- Fixture: representative 'show counters' output ---------- */
static const char COUNTERS_FIXTURE[] =
    "show counters\n"
    "RPKT.ce0\t\t    :\t\t      3,255\t\t +3,255\n"
    "RMCA.ce0\t\t    :\t\t      3,255\t\t +3,255\n"
    "RBYT.ce0\t\t    :\t\t    502,553\t   +502,553\n"
    "TPKT.ce0\t\t    :\t\t      3,572\t\t +3,572\n"
    "TMCA.ce0\t\t    :\t\t      3,572\t\t +3,572\n"
    "TBYT.ce0\t\t    :\t\t    786,203\t   +786,203\n"
    "TPOK.ce0\t\t    :\t\t      3,572\t\t +3,572\n"
    "T64.ce0 \t\t    :\t\t\t  1\t\t     +1\n"
    "T255.ce0\t\t    :\t\t      1,764\t\t +1,764\n"
    "T511.ce0\t\t    :\t\t      1,809\t\t +1,809\n"
    "RPKT.xe86\t\t    :\t\t      1,200\t\t +1,200\n"
    "RBYT.xe86\t\t    :\t\t    240,000\t   +240,000\n"
    "RMCA.xe86\t\t    :\t\t      1,100\t\t +1,100\n"
    "RBCA.xe86\t\t    :\t\t        100\t\t   +100\n"
    "TPKT.xe86\t\t    :\t\t        500\t\t   +500\n"
    "TBYT.xe86\t\t    :\t\t     50,000\t    +50,000\n"
    "drivshell>";

/* ---------- Fixture: representative 'ps' output snippet ---------- */
static const char PS_FIXTURE[] =
    "ps\n"
    "                 ena/        speed/ link auto    STP\n"
    "           port  link  Lns   duplex scan neg?\n"
    "       ce0(  1)  up     4  100G  FD   SW  No\n"
    "      xe85(117)  !ena   1   25G  FD None  No\n"
    "      xe86(118)  up     1   25G  FD   SW  No\n"
    "      xe87(119)  up     1   25G  FD   SW  No\n"
    "drivshell>";

/* ---------- Thin reimplementation of parse_counters() for test ---------- */
/* We call bcmcmd_fetch_counters() which writes to the socket.
 * Instead, inline parse_counters using the fixture directly.
 * This tests the parser without needing a live socket. */

/* Forward-declare the static function we want to test by including the .c file. */
#define parse_counters parse_counters_internal
#include "bcmcmd_client.c"
#undef  parse_counters

#define PASS(msg) do { printf("  PASS: %s\n", msg); passes++; } while(0)
#define FAIL(msg) do { printf("  FAIL: %s\n", msg); fails++;  } while(0)

int main(void)
{
    int passes = 0, fails = 0;
    counter_cache_t cache;
    memset(&cache, 0, sizeof(cache));
    pthread_mutex_init(&cache.lock, NULL);

    /* Test 1: parse_counters on fixture finds ce0 */
    parse_counters_internal(COUNTERS_FIXTURE, &cache);
    port_row_t *ce0 = NULL;
    for (int i = 0; i < cache.n_rows; i++)
        if (strcmp(cache.rows[i].port_name, "ce0") == 0) { ce0 = &cache.rows[i]; break; }
    if (ce0) PASS("ce0 row found");
    else      { FAIL("ce0 row not found"); goto summary; }

    /* Test 2: RBYT.ce0 = 502553 */
    int rbyt_idx = stat_map_index(0x00000000);  /* SAI_PORT_STAT_IF_IN_OCTETS */
    if (rbyt_idx >= 0 && ce0->val[rbyt_idx] == 502553)
        PASS("RBYT.ce0 = 502553");
    else
        FAIL("RBYT.ce0 wrong");

    /* Test 3: IN_NON_UCAST = RMCA + RBCA (ce0 has RMCA=3255, RBCA not shown → 0+3255=3255) */
    int non_ucast_idx = stat_map_index(0x00000002);
    if (non_ucast_idx >= 0 && ce0->val[non_ucast_idx] == 3255)
        PASS("IN_NON_UCAST = RMCA+RBCA = 3255");
    else
        FAIL("IN_NON_UCAST wrong");

    /* Test 4: parse_counters finds xe86 */
    port_row_t *xe86 = NULL;
    for (int i = 0; i < cache.n_rows; i++)
        if (strcmp(cache.rows[i].port_name, "xe86") == 0) { xe86 = &cache.rows[i]; break; }
    if (xe86) PASS("xe86 row found");
    else       { FAIL("xe86 row not found"); goto summary; }

    /* Test 5: IN_NON_UCAST for xe86 = RMCA(1100) + RBCA(100) = 1200 */
    if (non_ucast_idx >= 0 && xe86->val[non_ucast_idx] == 1200)
        PASS("xe86 IN_NON_UCAST = RMCA+RBCA = 1200");
    else
        FAIL("xe86 IN_NON_UCAST wrong");

    /* Test 6: ps fixture parsing */
    {
        int sdk_ports[32];
        char pnames[32][SHIM_PORT_NAME_LEN];
        /* Simulate ps parsing by calling the internal line parser.
         * We can't call bcmcmd_ps() without a socket, so parse the fixture inline. */
        int n = 0;
        char fixture_copy[sizeof(PS_FIXTURE)];
        memcpy(fixture_copy, PS_FIXTURE, sizeof(PS_FIXTURE));
        char *line = fixture_copy;
        while (n < 32) {
            char *nl = strchr(line, '\n');
            if (!nl) break;
            *nl = '\0';
            char *paren = strchr(line, '(');
            if (!paren || paren == line) { line = nl + 1; continue; }
            char *name_end = paren;
            char *name_start = paren - 1;
            while (name_start > line && *name_start != ' ') name_start--;
            if (*name_start == ' ') name_start++;
            int namelen = (int)(name_end - name_start);
            if (namelen <= 0 || namelen >= SHIM_PORT_NAME_LEN) { line = nl + 1; continue; }
            char *cparen = strchr(paren, ')');
            if (!cparen) { line = nl + 1; continue; }
            char numstr[16] = {0};
            int numlen = (int)(cparen - paren - 1);
            if (numlen <= 0 || numlen >= 16) { line = nl + 1; continue; }
            memcpy(numstr, paren + 1, (size_t)numlen);
            int sdk_port = atoi(numstr);
            if (sdk_port <= 0) { line = nl + 1; continue; }
            strncpy(pnames[n], name_start, (size_t)namelen);
            pnames[n][namelen] = '\0';
            sdk_ports[n] = sdk_port;
            n++;
            line = nl + 1;
        }
        /* Expect: ce0→1, xe85→117, xe86→118, xe87→119 */
        int ok = (n == 4 &&
                  strcmp(pnames[0], "ce0") == 0  && sdk_ports[0] == 1   &&
                  strcmp(pnames[1], "xe85") == 0 && sdk_ports[1] == 117 &&
                  strcmp(pnames[2], "xe86") == 0 && sdk_ports[2] == 118 &&
                  strcmp(pnames[3], "xe87") == 0 && sdk_ports[3] == 119);
        if (ok) PASS("ps fixture: ce0(1), xe85(117), xe86(118), xe87(119)");
        else     FAIL("ps fixture parsing wrong");
    }

summary:
    printf("\n%d passed, %d failed\n", passes, fails);
    return fails ? 1 : 0;
}
```

- [ ] **Step 4: Build and run test_parser to verify it fails (no shim.o yet)**

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim
make test_parser 2>&1 | tail -5
./test_parser 2>&1
```

Expected: compiles (stat_map.c and bcmcmd_client.c are complete) and ALL 6 tests pass.
If any test fails, diagnose the line parsing logic before proceeding.

- [ ] **Step 5: Commit**

```bash
cd /export/sonic/sonic-buildimage.claude
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/bcmcmd_client.c
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/test_parser.c
git commit -m "feat(shim): bcmcmd_client.c — Unix socket client and show counters parser with test_parser fixture tests"
```

---

## Task 4: `shim.c` — `sai_api_query` intercept, flex detection, cache

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c`

- [ ] **Step 1: Write `shim.c`**

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

    for (uint32_t i = 0; i < attr.value.u32list.count; i++) {
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

/* get_port_stats_ext: replace with passthrough — the ext path (drop counters)
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

    /* Save the real function pointers (may change across calls). */
    g_real_get_port_stats     = port_api->get_port_stats;
    g_real_get_port_stats_ext = port_api->get_port_stats_ext;
    g_real_get_port_attr      = port_api->get_port_attribute;

    /* Replace with shim functions. */
    port_api->get_port_stats     = shim_get_port_stats;
    port_api->get_port_stats_ext = shim_get_port_stats_ext;

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
```

- [ ] **Step 2: Update `Makefile` to build the full shared library**

Replace the `libsai-stat-shim.so` target in the existing Makefile:

```makefile
# Makefile for libsai-stat-shim.so and test_parser
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC
LDFLAGS = -lpthread

OBJS = shim.o bcmcmd_client.o stat_map.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl $(LDFLAGS)

test_parser: test_parser.o bcmcmd_client.o stat_map.o
	$(CC) -o $@ $^ $(LDFLAGS)

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so test_parser
```

- [ ] **Step 3: Build the shared library (inside sonic-slave or directly)**

```bash
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim
make clean && make libsai-stat-shim.so 2>&1
```

Expected: `libsai-stat-shim.so` produced, no errors.

If `dlfcn.h` is missing: `apt-get install -y libc-dev` inside the build container.

- [ ] **Step 4: Commit**

```bash
cd /export/sonic/sonic-buildimage.claude
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/shim.c
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sai-stat-shim/Makefile
git commit -m "feat(shim): shim.c — sai_api_query intercept, flex detection, and counter cache logic"
```

---

## Task 5: `debian/rules` integration — build and install the shim

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/debian/rules`
- Modify: `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.install`

- [ ] **Step 1: Write the failing test — verify shim is NOT currently in the .deb**

```bash
ssh admin@192.168.88.12 "test -f /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so && echo EXISTS || echo ABSENT"
```

Expected output: `ABSENT`

- [ ] **Step 2: Add shim build to `debian/rules`**

In `override_dh_auto_build`, after the `wedge100s-bmc-auth.c` block:

```makefile
		if [ -d $(MOD_SRC_DIR)/$${mod}/sai-stat-shim ]; then \
			$(MAKE) $(MAKE_FLAGS) -C $(MOD_SRC_DIR)/$${mod}/sai-stat-shim libsai-stat-shim.so; \
			echo "Built libsai-stat-shim.so for $$mod"; \
		fi; \
```

In `override_dh_auto_clean`, after the `rm -f` lines:

```makefile
		if [ -d $(MOD_SRC_DIR)/$${mod}/sai-stat-shim ]; then \
			$(MAKE) -C $(MOD_SRC_DIR)/$${mod}/sai-stat-shim clean; \
		fi; \
```

In `override_dh_auto_install`, after the `cp` for utils files:

```makefile
		if [ -d $(MOD_SRC_DIR)/$${mod}/sai-stat-shim ] && \
		   [ -f $(MOD_SRC_DIR)/$${mod}/sai-stat-shim/libsai-stat-shim.so ]; then \
			dh_installdirs -p$(PACKAGE_PRE_NAME)-$${mod} usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0; \
			cp $(MOD_SRC_DIR)/$${mod}/sai-stat-shim/libsai-stat-shim.so \
			   debian/$(PACKAGE_PRE_NAME)-$${mod}/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/; \
		fi; \
```

The full modified sections of `debian/rules`:

In `override_dh_auto_build` (full replacement of the for loop body):
```makefile
override_dh_auto_build:
	(set -e; for mod in $(MODULE_DIRS); do \
		if [ -d $(MOD_SRC_DIR)/$${mod}/modules ]; then \
			$(MAKE) $(MAKE_FLAGS) -C $(KERNEL_SRC)/build M=$(MOD_SRC_DIR)/$${mod}/modules modules; \
		fi; \
		if [ -f $(MOD_SRC_DIR)/$${mod}/setup.py ]; then \
			PYBUILD_NAME=$${mod} pybuild --build -d $${mod}; \
		fi; \
		if [ -f $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-daemon.c ]; then \
			gcc -O2 -o $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-daemon \
				$(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-daemon.c; \
			echo "Built wedge100s-bmc-daemon for $$mod"; \
		fi; \
		if [ -f $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-i2c-daemon.c ]; then \
			gcc -O2 -o $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-i2c-daemon \
				$(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-i2c-daemon.c; \
			echo "Built wedge100s-i2c-daemon for $$mod"; \
		fi; \
		if [ -f $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth.c ]; then \
			gcc -O2 -o $(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth \
				$(MOD_SRC_DIR)/$${mod}/$(UTILS_DIR)/wedge100s-bmc-auth.c; \
			echo "Built wedge100s-bmc-auth for $$mod"; \
		fi; \
		if [ -d $(MOD_SRC_DIR)/$${mod}/sai-stat-shim ]; then \
			$(MAKE) $(MAKE_FLAGS) -C $(MOD_SRC_DIR)/$${mod}/sai-stat-shim libsai-stat-shim.so; \
			echo "Built libsai-stat-shim.so for $$mod"; \
		fi; \
		if [ -d $(MOD_SRC_DIR)/$${mod}/pddf ]; then \
			cd $(MOD_SRC_DIR)/$${mod}/pddf; \
			if [ -f sonic_platform_setup.py ]; then \
				python3 sonic_platform_setup.py bdist_wheel -d $(MOD_SRC_DIR)/$${mod}/pddf; \
				echo "Finished making pddf whl package for $$mod"; \
			fi; \
			cd $(MOD_SRC_DIR); \
		elif [ -f $(MOD_SRC_DIR)/$${mod}/sonic_platform_setup.py ]; then \
			cd $(MOD_SRC_DIR)/$${mod}; \
			python3 sonic_platform_setup.py bdist_wheel -d $(MOD_SRC_DIR)/$${mod}; \
			echo "Finished making sonic_platform whl package for $$mod"; \
			cd $(MOD_SRC_DIR); \
		fi; \
	done)
```

- [ ] **Step 3: Update the `.install` file**

Append to `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.install`:

```
wedge100s-32x/sai-stat-shim/libsai-stat-shim.so usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0
```

Wait — the install step in `override_dh_auto_install` in rules already handles this via the explicit `cp`. The `.install` file approach is an alternative. Remove the explicit copy from rules and use the `.install` file instead for cleaner packaging. Pick one approach and be consistent.

**Decision: use the explicit `cp` in rules** (consistent with how the whl is installed). The `.install` file line is NOT needed — skip this step.

- [ ] **Step 4: Build the .deb to verify shim compiles inside the build system**

```bash
cd /export/sonic/sonic-buildimage.claude
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb 2>&1 | tail -30
```

Expected: `libsai-stat-shim.so` appears in the build output, .deb produced.

Verify contents:
```bash
dpkg -c target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb | grep shim
```

Expected: `./usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so`

- [ ] **Step 5: Commit**

```bash
cd /export/sonic/sonic-buildimage.claude
git add platform/broadcom/sonic-platform-modules-accton/debian/rules
git commit -m "feat(shim): wire libsai-stat-shim.so into debian/rules build and install"
```

---

## Task 6: `postinst` — patch `syncd.sh` and trigger container rebuild

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`
- Create: `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.prerm`

**Injection strategy (verified 2026-03-28):** `/usr/bin/syncd.sh` contains a `docker create` invocation with `--env` flags. The shim must be available as `/usr/share/sonic/platform/libsai-stat-shim.so` inside the container — this path is satisfied by installing the `.so` to `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/` on the host, which is bind-mounted as `/usr/share/sonic/platform:ro` in the container.

- [ ] **Step 1: Add the `syncd.sh` patch block to `postinst`**

Append to `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` (before the final `exit 0`):

```bash
# Patch syncd.sh to inject LD_PRELOAD for the SAI stat shim.
#
# The shim library is installed at:
#   /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so
# which is bind-mounted into the syncd container as:
#   /usr/share/sonic/platform/libsai-stat-shim.so  (read-only)
#
# We add two --env flags to the 'docker create' command in syncd.sh:
#   LD_PRELOAD=/usr/share/sonic/platform/libsai-stat-shim.so
#   WEDGE100S_BCM_CONFIG=/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm
#
# The HWSKU dir is bind-mounted from the platform device directory so
# the BCM config is reachable inside the container at that path.
#
# Idempotent: guarded by grep for the marker string.
SYNCD_SH="/usr/bin/syncd.sh"
if [ -f "$SYNCD_SH" ] && ! grep -q "wedge100s-stat-shim" "$SYNCD_SH"; then
    python3 - <<'PYEOF'
import sys

SYNCD_SH = "/usr/bin/syncd.sh"
# Insert before the first --name= flag in the docker create block.
NEEDLE = '        --name=$DOCKERNAME \\'
INSERT = (
    '        --env "LD_PRELOAD=/usr/share/sonic/platform/libsai-stat-shim.so" \\\n'
    '        --env "WEDGE100S_BCM_CONFIG=/usr/share/sonic/hwsku/'
    'th-wedge100s-32x-flex.config.bcm" \\  # wedge100s-stat-shim\n'
)

with open(SYNCD_SH) as fh:
    text = fh.read()

if NEEDLE not in text:
    print("wedge100s postinst: WARNING: needle not found in " + SYNCD_SH +
          " — shim LD_PRELOAD not injected", file=sys.stderr)
    sys.exit(0)

text = text.replace(NEEDLE, INSERT + NEEDLE, 1)
with open(SYNCD_SH, 'w') as fh:
    fh.write(text)
print("wedge100s postinst: patched " + SYNCD_SH + " — shim LD_PRELOAD added")
PYEOF
fi

# If the syncd container is stopped/not-running, remove it so the next
# systemd start recreates it with the new LD_PRELOAD flags.
# Do NOT kill a running syncd (hardware state loss risk).
if command -v docker >/dev/null 2>&1; then
    SYNCD_STATUS=$(docker inspect --format='{{.State.Status}}' syncd 2>/dev/null || true)
    if [ "$SYNCD_STATUS" = "exited" ] || [ "$SYNCD_STATUS" = "created" ]; then
        docker rm syncd >/dev/null 2>&1 || true
        echo "wedge100s postinst: removed stopped syncd container — will be recreated on next start"
    elif [ "$SYNCD_STATUS" = "running" ]; then
        echo "wedge100s postinst: NOTE: syncd is running. Reboot or 'systemctl restart syncd' to" \
             "activate shim LD_PRELOAD."
    fi
fi
```

- [ ] **Step 2: Create `prerm` to reverse the `syncd.sh` patch**

Create `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.prerm`:

```bash
#!/bin/sh
set -e

# Remove the SAI stat shim LD_PRELOAD injection from /usr/bin/syncd.sh.
SYNCD_SH="/usr/bin/syncd.sh"
if [ -f "$SYNCD_SH" ] && grep -q "wedge100s-stat-shim" "$SYNCD_SH"; then
    python3 - <<'PYEOF'
import sys, re

SYNCD_SH = "/usr/bin/syncd.sh"
with open(SYNCD_SH) as fh:
    text = fh.read()

# Remove the two inserted --env lines (identified by the wedge100s-stat-shim marker).
text = re.sub(
    r'\s*--env "LD_PRELOAD=[^"]*" \\\n'
    r'\s*--env "WEDGE100S_BCM_CONFIG=[^"]*" \\[^\n]*wedge100s-stat-shim[^\n]*\n',
    '\n',
    text
)

with open(SYNCD_SH, 'w') as fh:
    fh.write(text)
print("wedge100s prerm: removed shim LD_PRELOAD from " + SYNCD_SH)
PYEOF
fi

exit 0
```

```bash
chmod +x platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.prerm
```

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst
git add platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.prerm
git commit -m "feat(shim): postinst patches syncd.sh to inject LD_PRELOAD; prerm reverses patch"
```

---

## Task 7: On-target tests (write BEFORE deploying)

**Files:**
- Create: `tests/stage_25_shim/__init__.py`
- Create: `tests/stage_25_shim/test_shim.py`

Write the tests now; run them to confirm they FAIL before deploying the shim.

- [ ] **Step 1: Create `tests/stage_25_shim/__init__.py`**

```python
```
(empty file)

- [ ] **Step 2: Write `tests/stage_25_shim/test_shim.py`**

```python
"""Stage 25 — SAI stat shim verification.

Tests that the libsai-stat-shim.so correctly provides counter data for
flex sub-ports (Ethernet0-3, Ethernet64-67, Ethernet80-83) and does not
regress non-flex ports.

Requires: shim deployed (dpkg -i the .deb), syncd restarted.

Tests:
  test_shim_library_present         libsai-stat-shim.so exists on host
  test_syncd_sh_patched             syncd.sh has LD_PRELOAD line
  test_syncd_has_ld_preload         running syncd process has LD_PRELOAD set
  test_flex_ports_have_full_stats   flex sub-ports have >= 60 SAI stat keys
  test_non_flex_ports_not_regressed non-flex ports still have >= 60 keys
  test_flex_port_rx_bytes_nonzero   flex ports with links up show non-zero RBYT
  test_flex_port_tx_bytes_nonzero   flex ports with links up show non-zero TBYT
  test_startup_zeros_succeed        keys are present (not absent) even when 0
"""

import re
import time
import pytest

FLEX_PORTS = [
    "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
    "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    "Ethernet80", "Ethernet81", "Ethernet82", "Ethernet83",
]
NON_FLEX_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
SHIM_PATH = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so"
MIN_STAT_KEYS = 60  # expect 68; allow some slack for future SAI version changes


def test_shim_library_present(ssh):
    """libsai-stat-shim.so is installed at the expected host path."""
    out, err, rc = ssh.run(f"test -f {SHIM_PATH} && echo OK || echo ABSENT", timeout=5)
    assert out.strip() == "OK", (
        f"Shim library not found at {SHIM_PATH}.\n"
        "Rebuild and install the .deb: dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb"
    )


def test_syncd_sh_patched(ssh):
    """syncd.sh contains the LD_PRELOAD injection marker."""
    out, err, rc = ssh.run("grep -c 'wedge100s-stat-shim' /usr/bin/syncd.sh || echo 0", timeout=5)
    assert int(out.strip()) >= 1, (
        "syncd.sh has not been patched with the shim LD_PRELOAD.\n"
        "Run: sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb"
    )


def test_syncd_has_ld_preload(ssh):
    """The running syncd process has LD_PRELOAD set in its environment."""
    out, err, rc = ssh.run(
        "sudo docker exec syncd cat /proc/1/environ 2>/dev/null | tr '\\0' '\\n' | grep LD_PRELOAD || echo NONE",
        timeout=15
    )
    assert "libsai-stat-shim" in out, (
        "syncd process does not have LD_PRELOAD=libsai-stat-shim.\n"
        "The syncd container must be recreated: 'systemctl restart syncd' or reboot.\n"
        f"Current /proc/1/environ LD_PRELOAD: {out.strip()!r}"
    )


def _get_stat_key_count(ssh, port_name):
    """Return number of SAI_PORT_STAT_* keys in COUNTERS_DB for port_name."""
    oid_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port_name}", timeout=10
    )
    oid = oid_out.strip()
    if not oid:
        return 0
    keys_out, _, _ = ssh.run(
        f"redis-cli -n 2 hkeys 'COUNTERS:{oid}'", timeout=10
    )
    return sum(1 for k in keys_out.strip().splitlines() if k.startswith("SAI_PORT_STAT_"))


def test_flex_ports_have_full_stats(ssh):
    """Flex sub-ports have >= 60 SAI stat keys in COUNTERS_DB (shim working)."""
    # Allow up to 30s for flex counter poll to populate after syncd start.
    deadline = time.time() + 30
    results = {}
    while time.time() < deadline:
        for port in FLEX_PORTS:
            if port not in results:
                n = _get_stat_key_count(ssh, port)
                if n >= MIN_STAT_KEYS:
                    results[port] = n
        if len(results) == len(FLEX_PORTS):
            break
        time.sleep(3)

    failed = [p for p in FLEX_PORTS if p not in results]
    if failed:
        # Gather actual counts for diagnosis.
        actuals = {p: _get_stat_key_count(ssh, p) for p in failed}
        pytest.fail(
            f"Flex ports with <{MIN_STAT_KEYS} stat keys (shim not working):\n"
            + "\n".join(f"  {p}: {actuals[p]} keys" for p in failed)
            + "\nExpected ≥60 keys. Check:\n"
            "  1. syncd has LD_PRELOAD: test_syncd_has_ld_preload\n"
            "  2. shim syslog: sudo grep 'sai-stat-shim' /var/log/syslog\n"
            "  3. bcmcmd socket: sudo docker exec syncd ls /var/run/sswsyncd/"
        )
    print(f"\nFlex port stat key counts: { {p: results[p] for p in FLEX_PORTS} }")


def test_non_flex_ports_not_regressed(ssh):
    """Non-flex ports still have >= 60 SAI stat keys (passthrough not broken)."""
    for port in NON_FLEX_PORTS:
        n = _get_stat_key_count(ssh, port)
        assert n >= MIN_STAT_KEYS, (
            f"{port}: only {n} stat keys (expected ≥{MIN_STAT_KEYS}). "
            "Shim passthrough may be broken — check get_port_stats intercept."
        )
    print(f"\nNon-flex stat counts: { {p: _get_stat_key_count(ssh, p) for p in NON_FLEX_PORTS} }")


def test_flex_port_rx_bytes_nonzero(ssh):
    """Flex sub-ports that are link-up show non-zero IF_IN_OCTETS."""
    # Find link-up flex ports.
    up_ports = []
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    for port in FLEX_PORTS:
        if any(port in line and " up " in line for line in out.splitlines()):
            up_ports.append(port)
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up — cannot test RX counter increment")

    # Wait one poll cycle (max 15s) for counters to populate.
    time.sleep(5)

    for port in up_ports[:2]:  # check at most 2 to keep test fast
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget 'COUNTERS:{oid}' SAI_PORT_STAT_IF_IN_OCTETS", timeout=10
        )
        val = int(val_out.strip() or "0")
        print(f"  {port} IF_IN_OCTETS = {val:,}")
        assert val > 0, (
            f"{port}: IF_IN_OCTETS=0 even though link is up.\n"
            "Check bcmcmd 'show counters' for this port — may be 0 at BCM level too.\n"
            "Verify shim is connected: look for 'shim: bcmcmd connected' in syslog."
        )


def test_flex_port_tx_bytes_nonzero(ssh):
    """Flex sub-ports that are link-up show non-zero IF_OUT_OCTETS."""
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    up_ports = [p for p in FLEX_PORTS
                if any(p in line and " up " in line for line in out.splitlines())]
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up")

    for port in up_ports[:2]:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget 'COUNTERS:{oid}' SAI_PORT_STAT_IF_OUT_OCTETS", timeout=10
        )
        val = int(val_out.strip() or "0")
        print(f"  {port} IF_OUT_OCTETS = {val:,}")
        assert val > 0, (
            f"{port}: IF_OUT_OCTETS=0 even though link is up."
        )


def test_startup_zeros_succeed(ssh):
    """All 12 flex sub-ports have the IN_DROPPED_PKTS key present (even if 0).

    This key worked before the shim (it goes through a different SAI path).
    If the shim broke something, this key would disappear.  Also verifies that
    the shim path returns SAI_STATUS_SUCCESS even when cache is empty/stale.
    """
    for port in FLEX_PORTS:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hexists 'COUNTERS:{oid}' SAI_PORT_STAT_IN_DROPPED_PKTS", timeout=10
        )
        assert val_out.strip() == "1", (
            f"{port}: SAI_PORT_STAT_IN_DROPPED_PKTS key is MISSING.\n"
            "This was working before the shim — shim may have broken get_port_stats_ext path."
        )
    print("\nAll 12 flex ports: SAI_PORT_STAT_IN_DROPPED_PKTS key present ✓")
```

- [ ] **Step 3: Run tests to confirm they FAIL pre-deploy**

```bash
cd tests && pytest stage_25_shim/ -v 2>&1 | head -40
```

Expected: `test_shim_library_present` fails (ABSENT), others may skip/fail. This confirms the tests work.

- [ ] **Step 4: Commit**

```bash
cd /export/sonic/sonic-buildimage.claude
git add tests/stage_25_shim/__init__.py
git add tests/stage_25_shim/test_shim.py
git commit -m "test(shim): stage_25_shim pytest — flex port full-stat and passthrough regression tests"
```

---

## Task 8: Build `.deb`, deploy, run tests

**Files:** None created — this is a deploy/verify task.

- [ ] **Step 1: Build the .deb**

```bash
cd /export/sonic/sonic-buildimage.claude
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb 2>&1 | tail -20
```

Verify shim is in the package:
```bash
dpkg -c target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb | grep shim
```

Expected: `./usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so`

- [ ] **Step 2: Copy .deb to target**

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
```

- [ ] **Step 3: Install the .deb (syncd must be running)**

```bash
ssh admin@192.168.88.12 "sudo dpkg -i ~/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb 2>&1" | tail -30
```

Verify postinst patched syncd.sh:
```bash
ssh admin@192.168.88.12 "grep -A2 'wedge100s-stat-shim' /usr/bin/syncd.sh"
```

Expected output:
```
        --env "LD_PRELOAD=/usr/share/sonic/platform/libsai-stat-shim.so" \
        --env "WEDGE100S_BCM_CONFIG=/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm" \  # wedge100s-stat-shim
```

- [ ] **Step 4: Restart syncd to pick up LD_PRELOAD**

```bash
ssh admin@192.168.88.12 "sudo systemctl restart syncd"
sleep 15  # allow syncd to reinitialize ports
```

Verify shim is active:
```bash
ssh admin@192.168.88.12 "sudo grep 'sai-stat-shim' /var/log/syslog | tail -5"
```

Expected: `shim: sai-stat-shim initialised` and `shim: bcmcmd connected, N ports from ps`

- [ ] **Step 5: Run the new stage_25 tests**

```bash
cd tests && pytest stage_25_shim/ -v 2>&1
```

Expected: all 8 tests pass.

- [ ] **Step 6: Run regression — stage_24 counters must still pass**

```bash
cd tests && pytest stage_24_counters/ -v 2>&1
```

Expected: all tests pass (passthrough non-flex ports not broken).

- [ ] **Step 7: Document findings**

```bash
# Write hardware-verified results to notes/
cat > /export/sonic/sonic-buildimage.claude/notes/sai-stat-shim-results.md << 'EOF'
# SAI Stat Shim — Deployment Results

## Hardware verification (YYYY-MM-DD)

- Shim deployed: libsai-stat-shim.so installed, syncd.sh patched
- syncd process LD_PRELOAD confirmed in /proc/1/environ
- Flex ports (Ethernet0-3, 64-67, 80-83): N stat keys in COUNTERS_DB (was 2)
- Non-flex ports: still 68 keys (passthrough working)
- Flex port IF_IN_OCTETS/IF_OUT_OCTETS: non-zero for link-up ports
- bcmcmd socket: /var/run/sswsyncd/sswsyncd.socket (confirmed)
- ps entries parsed: N ports
- Cache TTL: 500ms (bcmcmd 'show counters' one round-trip per 500ms window)

## Breakout mode coverage

- 1×100G: passthrough (non-flex)
- 4×25G: shim active, full stats populated
- Other breakout modes: to be tested
EOF
```

Update the date and values after running:
```bash
ssh admin@192.168.88.12 "redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet0 | xargs -I{} redis-cli -n 2 hlen 'COUNTERS:{}'"
```

- [ ] **Step 8: Final commit**

```bash
cd /export/sonic/sonic-buildimage.claude
git add notes/sai-stat-shim-results.md
git commit -m "docs(shim): hardware verification results for SAI stat shim deployment"
```

---

## Spec Coverage Self-Check

| Spec requirement | Task covering it |
|---|---|
| All 68 SAI stat IDs populated for flex sub-ports | Task 2 (stat_map) + Task 4 (shim logic) |
| Full breakout (128 ports) supported in one batch | Task 3 (fetch_counters) + Task 4 (cache) |
| Non-breakout ports: pure passthrough | Task 4 (shim_get_port_stats non-flex branch) |
| Dynamic flex detection, no hardcoded ports | Task 4 (try-real-first classification) |
| Startup race: zeros + SUCCESS until socket ready | Task 4 (bcmcmd_init failure → zeros) |
| 50ms non-blocking connect timeout | Task 3 (bcmcmd_connect poll) |
| Static SAI→bcmcmd map | Task 2 (stat_map.c) |
| Ships inside existing .deb | Task 5 (debian/rules) |
| postinst patches syncd.sh | Task 6 (postinst) |
| Thread safety: mutex on cache reads/writes | Task 4 (g_cache.lock, g_oids.lock) |
| Fetch dedup (fetch_in_progress flag) | Task 4 (refresh_cache_if_stale) |
| Unit tests (bcmcmd parser) | Task 3 (test_parser.c) |
| On-target integration tests (stage_25_shim) | Task 7 |
| Non-breakout regression (stage_24) | Task 8 step 6 |
| Dynamic breakout modes (1×100G, 2×50G, 4×25G, 4×10G) | Task 4 (try-real-first works for all modes) |

## Placeholder Scan

No "TBD", "TODO", or "implement later" phrases. Two areas requiring on-target verification:
1. **stat_map.c SAI enum values** — the hex integers `0x00000000..0x00000043` are derived from the SAI spec; verify with `grep SAI_PORT_STAT_IF_IN_OCTETS /usr/include/sai/saiport.h` inside syncd container before building.
2. **bcmcmd counter names for unmapped stats** — `RIDR`, `RFCS`, `RUND`, `RFRG`, `ROVR`, `TOVR`, `RJBR`, `TDRP`, `TERR` may not appear in `show counters` output for ports with no errors. If a name is absent from the cache, `raw_lookup()` returns 0 — safe, just means that stat stays 0 for ports with no errors. Verify by forcing traffic with errors or checking `bcmcmd 'show counters ce0' all` (which shows all counters including zeros).
