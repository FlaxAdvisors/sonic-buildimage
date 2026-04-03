# Design: Replace bcmcmd Counter Path with Direct bcm_stat_get

**Date:** 2026-04-03
**Status:** Approved
**Replaces:** bcmcmd socket path in sai-stat-shim (from 2026-03-27 design)

## Problem

The sai-stat-shim fetches flex sub-port (4x25G breakout) counters by connecting to
the bcmcmd diag shell socket, sending `show counters`, and parsing text output. This
path has three critical failures discovered in the 2026-04-03 session:

1. **Socket contention** — dsserve socket (backlog=1) blocks other bcmcmd users
2. **3-second banner timeout** — blocks syncd threads, causes orchagent SIGABRT
3. **Diag shell death** — becomes unresponsive ~60s after syncd init

A connect backoff (SHIM_CONNECT_BACKOFF_MS=5000) was added as mitigation, but the
fundamental fix is to bypass bcmcmd entirely.

## Discovery

`bcm_stat_get` and `bcm_stat_multi_get` are exported dynamic symbols in
`/usr/lib/libsai.so.1.0` (530MB, contains the full BCM SDK). Since the shim is
LD_PRELOAD'd into syncd (same address space), these can be called directly via
`dlsym(RTLD_DEFAULT, "bcm_stat_multi_get")`.

Key properties:
- Returns absolute monotonic totals from the SDK's DMA counter buffer
- Microsecond-level reads (no socket, no text parsing)
- Works for any valid SDK port number, including flex sub-ports
- No new build dependencies (enum values are integer constants)

## Approach

**Approach A (chosen):** Call `bcm_stat_multi_get` directly via dlsym.

Rejected alternatives:
- `/dev/mem` SCHAN register reads: BCM SDK register map is proprietary; counter
  tables require SCHAN protocol, not simple MMIO reads. Impractical without SDK source.
- Fix bcmcmd reliability: Cannot fix the 60s diag shell death (BCM SDK limitation).

## Architecture

### BCM API Integration

New function pointer resolved at init:

```c
typedef int (*bcm_stat_multi_get_fn)(int unit, int port, int nstat,
                                      int *stat_arr, uint64_t *value_arr);
static bcm_stat_multi_get_fn g_bcm_stat_multi_get = NULL;
```

Resolved in `sai_api_query()` via `dlsym(RTLD_DEFAULT, "bcm_stat_multi_get")`.

### bcm_stat_val_t Enum Stubs

Define only the ~25 values needed, as integer constants in shim.h:

```c
enum {
    snmpIfInOctets          = 0,
    snmpIfInUcastPkts       = 1,
    snmpIfInNUcastPkts      = 2,
    snmpIfInDiscards        = 3,
    snmpIfInErrors          = 4,
    snmpIfInUnknownProtos   = 5,
    snmpIfOutOctets         = 6,
    snmpIfOutUcastPkts      = 7,
    snmpIfOutNUcastPkts     = 8,
    snmpIfOutDiscards       = 9,
    snmpIfOutErrors         = 10,
    snmpIfOutQLen           = 11,
    snmpEtherStatsUndersizePkts   = 15,
    snmpEtherStatsFragments       = 16,
    snmpEtherStatsPkts64Octets    = 17,
    snmpEtherStatsPkts65to127Octets   = 18,
    snmpEtherStatsPkts128to255Octets  = 19,
    snmpEtherStatsPkts256to511Octets  = 20,
    snmpEtherStatsPkts512to1023Octets = 21,
    snmpEtherStatsPkts1024to1518Octets = 22,
    snmpEtherStatsOversizePkts    = 23,
    snmpEtherRxOversizePkts       = 24,
    snmpEtherTxOversizePkts       = 25,
    snmpEtherStatsJabbers         = 26,
    snmpEtherStatsTXNoErrors      = 34,
    snmpIfInBroadcastPkts         = 35,
    snmpIfInMulticastPkts         = 36,
    snmpIfOutBroadcastPkts        = 37,
    snmpIfOutMulticastPkts        = 38,
    snmpIfHCInOctets              = 39,
    snmpIfHCInUcastPkts           = 40,
    snmpIfHCInMulticastPkts       = 41,
    snmpIfHCInBroadcastPkts       = 42,
    snmpIfHCOutOctets             = 43,
    snmpIfHCOutUcastPkts          = 44,
    snmpIfHCOutMulticastPkts      = 45,
    snmpIfHCOutBroadcastPckts     = 46,
};
```

These are fixed by the BCM API specification and do not change across SDK versions.

### Stat Mapping Table

`stat_map_entry_t` changes from string-based to integer-based:

```c
typedef struct {
    sai_port_stat_t  stat_id;
    int              bcm_stat;    /* bcm_stat_val_t; -1 = return 0 */
    int              bcm_stat2;   /* second stat to add; -1 = none */
} stat_map_entry_t;
```

Examples:

| SAI stat | bcm_stat | bcm_stat2 |
|---|---|---|
| IF_IN_OCTETS (0) | snmpIfHCInOctets (39) | -1 |
| IF_IN_NON_UCAST_PKTS (2) | snmpIfHCInMulticastPkts (41) | snmpIfHCInBroadcastPkts (42) |
| IF_OUT_OCTETS (9) | snmpIfHCOutOctets (43) | -1 |

HC (64-bit high-capacity) variants used where available.

### Counter Read Path

Old:
```
shim_get_port_stats(oid)
  → refresh_cache()
    → bcmcmd_connect() → "show counters\n" → parse text → cache lookup
```

New:
```
shim_get_port_stats(oid)
  → build bcm_stat_val_t[] from requested SAI stat IDs
  → bcm_stat_multi_get(0, sdk_port, count, stat_arr, value_arr)
  → for dual-stat entries (bcm_stat2 != -1): add second bcm_stat value
  → return directly (absolute totals, no delta accumulation needed)
```

### Error Handling

- `dlsym` fails: log error at init, return zeros for all flex ports. No crash.
- `bcm_stat_multi_get` returns non-zero: log once per port, return zeros. No timeout.
- Both are clean degradation, same as current bcmcmd-unreachable behavior but
  without the 3s timeout penalty.

## Files Changed

| File | Action | Change |
|---|---|---|
| `shim.h` | Modify | Add bcm_stat_val_t enum stubs. Change stat_map_entry_t to int-based. Remove counter_cache_t, bcmcmd function declarations. Add bcm_stat_multi_get_fn typedef. |
| `stat_map.c` | Modify | Replace string mapping with bcm_stat_val_t integer mapping. |
| `shim.c` | Modify | Remove g_ps_map, g_cache, refresh_cache(), bcmcmd_init_ps(), backoff logic. Add g_bcm_stat_multi_get dlsym. Replace flex path with direct bcm_stat_multi_get call. |
| `bcmcmd_client.c` | Delete | Entire file removed. |
| `Makefile` | Modify | Remove bcmcmd_client.o from build. |

**Net change:** Remove ~330 lines (bcmcmd_client.c) + ~80 lines from shim.c/shim.h,
add ~80 lines. Net reduction ~330 lines.

## What Stays Unchanged

- `g_lane_map[]` + `parse_bcm_config()` — OID→sdk_port resolution
- `oid_cache_t` — flex vs non-flex classification
- `resolve_sdk_port()` — SAI HW_LANE_LIST query
- `shim_get_port_stats_ext` — passthrough unchanged
- `patch_fnptr()` / `mprotect` — function pointer patching
- Non-flex port passthrough to real `get_port_stats`

## Testing

### Risk Validation (First Priority)

Before full implementation, verify `bcm_stat_get` works for sub-ports with a
standalone binary run inside the syncd container:

```c
bcm_stat_get(0, 118, 39 /* snmpIfHCInOctets */, &val);
// If val > 0 and matches "show counters" RBYT for xe86 → confirmed
```

### Functional Verification

1. Start syncd, confirm shim loads and `bcm_stat_multi_get` resolves
2. Generate traffic on flex sub-ports (Ethernet100-103)
3. Compare `show interfaces counters` against peer EOS — values match
4. Confirm no orchagent SIGABRT

### Regression

- Non-flex ports still passthrough to real get_port_stats
- get_port_stats_ext passthrough unchanged
- Breakout reconfiguration (second sai_api_query) re-resolves OIDs
- stage_25_shim and stage_24_counters test suites pass
