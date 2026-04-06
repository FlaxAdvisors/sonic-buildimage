# Flex Counter / Breakout Port Stats — Status & Remaining Work
**Date:** 2026-04-05

## Problem Statement

SAI `get_port_stats` / `get_port_stats_ext` fails for breakout sub-ports on Tomahawk (Wedge100S). FlexCounter can't collect counters or compute rates for these ports. Native 4-lane (100G) ports work fine via SAI.

## Breakout Port Inventory

All breakout ports are <4 lanes. 12 total configured, 6 currently link-up:

| Ethernet | Lanes | SDK Port | bcmcmd Name | Link  | Notes |
|----------|-------|----------|-------------|-------|-------|
| 0        | 117   | 118      | xe86        | up    | 1-lane 25G breakout of QSFP30 |
| 1        | 118   | 119      | xe87        | up    | 1-lane 25G breakout of QSFP30 |
| 2        | 119   | 120      | xe88        | down  | |
| 3        | 120   | 121      | xe89        | down  | |
| 64       | 53    | 50       | xe36        | down  | |
| 65       | 54    | 51       | xe37        | down  | |
| 66       | 55    | 52       | xe38        | up    | 1-lane 10G breakout of QSFP14 |
| 67       | 56    | 53       | xe39        | up    | 1-lane 10G breakout of QSFP14 |
| 80       | 69    | 68       | xe49        | up    | 1-lane 25G breakout of QSFP18 |
| 81       | 70    | 69       | xe50        | up    | 1-lane 25G breakout of QSFP18 |
| 82       | 71    | 70       | xe51        | down  | |
| 83       | 72    | 71       | xe52        | down  | |

Port resolution chain (verified working 2026-04-05):
```
CONFIG_DB PORT|EthernetN → lanes → first_lane
BCM config portmap_X.0=lane:speed → SDK port
bcmcmd ps → SDK port → port name (xe38, ce0, ...)
bcmcmd show counters → port name → counter values
```

## Architecture: Daemon + Shim

### Python Daemon (`wedge100s-flex-counter-daemon.py`)
- Runs on **host** (not in syncd container)
- Connects to bcmcmd via `/var/run/docker-syncd/sswsyncd.socket`
- **Connect/disconnect each cycle** — does not hold the socket
- Polls `show counters` every 3s, accumulates hardware totals per port
- Writes to COUNTERS_DB (DB 2) via Redis HMSET for all breakout ports
- Writes binary cache file to `/var/run/docker-syncd/flex-counter-cache`
  (visible in syncd container as `/var/run/sswsyncd/flex-counter-cache`)

### SAI Shim (`libsai-stat-shim.so`)
- LD_PRELOAD'd into syncd process
- Intercepts `sai_api_query(SAI_API_PORT)` → patches `get_port_stats` and `get_port_stats_ext`
- On SAI success → passthrough (zero overhead for native ports)
- On SAI failure → reads daemon's binary cache file, fills values buffer with real counters, returns SUCCESS
- Goal: FlexCounter sees real values → computes rates/% for breakout ports

### Binary Cache File Format (`flex_counter_cache.h`)
```
Header:  magic(4) "FLEX" | version(4) | n_entries(4) | pad(4)
Entry:   oid(8) | stats[256](256×8)     — indexed by SAI stat enum value
```
Written atomically (write .tmp, rename).

## What Works Right Now

| Component | Status |
|-----------|--------|
| Daemon BCM config parsing (128 lane entries) | ✅ Working |
| Daemon bcmcmd ps parsing (128 ports, fixed `\(\s*(\d+)\)` regex) | ✅ Working |
| Daemon bcmcmd show counters parsing | ✅ Working |
| Daemon connect/disconnect per cycle (no socket monopolization) | ✅ Working |
| Daemon writes COUNTERS_DB for all 6 up breakout ports | ✅ Working |
| Daemon writes binary cache file (6 entries, correct OIDs) | ✅ Working |
| Shim loads via LD_PRELOAD (path fixed to bind-mount) | ✅ Working |
| Shim hooks sai_api_query(SAI_API_PORT) | ✅ Working |
| Counter values briefly visible in `show interfaces counters` | ✅ Partial |
| Rates computed by FlexCounter for breakout ports | ❌ Not working |

## Key Bug: FlexCounter Overwrites Daemon Values With Zeros

**Symptom:** Daemon writes real counter values to COUNTERS_DB. They appear briefly in `show interfaces counters`, then get overwritten to 0 by FlexCounter on its next poll cycle (~1s).

**Root cause under investigation.** Two theories:

### Theory A: SAI succeeds for breakout ports (returns 0)
After syncd restart, SAI may succeed for breakout ports but return zero counters (SDK counter baseline reset). The shim passes through on success. FlexCounter writes the zero SAI values, overwriting the daemon's real values.

If this is the case, the shim should NOT passthrough when values are all-zero — or FlexCounter should be removed for these ports.

### Theory B: Shim cache OID mismatch
Diagnostic logging (limited to `get_port_stats`, not `get_port_stats_ext`) showed only one OID failing: `0x100000054` with `count=1`. This is NOT a FlexCounter bulk stats call.

**Critical gap:** `get_port_stats_ext` was not instrumented. FlexCounter uses `get_port_stats_ext` when `STATS_MODE` is set. The actual failure/success behavior for breakout ports via `get_port_stats_ext` is unknown.

Diagnostic shim with `get_port_stats_ext` logging was built but not yet deployed.

### OID dynamics (confirmed)
SAI OIDs are assigned at syncd init and change on restart:
- Before restart: Ethernet66 = `oid:0x10000000005ce`
- After restart: native ports get new OIDs (`oid:0x1000000000001` etc.), breakout ports **kept the same OIDs** (confirmed in both COUNTERS_PORT_NAME_MAP and FLEX_COUNTER_TABLE)

The daemon reads OIDs from COUNTERS_PORT_NAME_MAP each cycle, so the cache file always has current OIDs. This is NOT the cause of the zero-overwrite bug.

## Remaining Work

### 1. Deploy `get_port_stats_ext` diagnostic (ready to deploy)
Build with ext logging is compiled. Deploy, restart syncd, examine logs to determine whether SAI succeeds or fails for breakout ports via the ext path.

### 2. Fix the zero-overwrite race (depends on #1 findings)

**If SAI fails for breakout ports (Theory B confirmed):**
- The shim cache read should work. Debug why `fill_from_cache` returns 0.
- Check OID byte ordering, file read errors, mmap issues.

**If SAI succeeds with zeros (Theory A confirmed):**
- Option A: Remove breakout ports from FlexCounter poll group (DB 5). Daemon writes both COUNTERS and RATES. This is the approach the previous Python daemon used.
- Option B: Make the shim detect "SAI success but stale/zero values" and substitute cache values. Fragile — hard to distinguish real zeros from stale zeros.
- Option C: Hybrid — let FlexCounter handle the ports where SAI works, daemon handles the rest. Requires reliable detection of which ports SAI fails for.

**Recommended:** Option A (remove from FlexCounter, daemon writes RATES). It's the simplest, has no race conditions, and was already proven working for Ethernet0/1 by the Python daemon.

### 3. Add RATES computation to daemon
If we go with Option A, the daemon must write `RATES:<oid>` to COUNTERS_DB:
- `RX_BPS`, `TX_BPS`, `RX_PPS`, `TX_PPS`
- `SAI_PORT_STAT_IF_IN_OCTETS_last`, `_OUT_OCTETS_last`, etc.

The Python daemon already has `write_rates()` (was working for Ethernet0/1). Just needs to be re-enabled and the `remove_from_flex_counter()` no-op replaced with actual DB 5 key deletion.

### 4. Persist across syncd restarts
The daemon runs on the host and survives syncd restarts. On restart:
- bcmcmd socket disappears briefly → daemon retries (already handled)
- COUNTERS_PORT_NAME_MAP repopulated → daemon picks up new OIDs next cycle
- FlexCounter re-adds breakout ports to poll group → daemon must re-remove them from DB 5

### 5. Port to C for performance
Once Python is stable and verified, port the daemon to C (daemon.c framework already exists, just needs rate computation and FlexCounter removal added).

## File Inventory

| File | Location | Purpose |
|------|----------|---------|
| `wedge100s-flex-counter-daemon.py` | `wedge100s-32x/utils/` | Python daemon (host) |
| `daemon.c` | `wedge100s-32x/flex-counter-daemon/` | C daemon (syncd, needs rates) |
| `bcmcmd_client.c/.h` | `wedge100s-32x/flex-counter-daemon/` | bcmcmd socket client |
| `stat_map.c/.h` | `wedge100s-32x/flex-counter-daemon/` | SAI↔BCM counter name mapping |
| `shim.c` | `wedge100s-32x/sai-stat-shim/` | LD_PRELOAD fault masker + cache reader |
| `shim.h` | `wedge100s-32x/sai-stat-shim/` | SAI type stubs |
| `flex_counter_cache.h` | `wedge100s-32x/sai-stat-shim/` | Binary cache file format (shared) |
| `compat.c` | `wedge100s-32x/sai-stat-shim/` | glibc __isoc23_sscanf compat |

## Deployment Notes

- Shim .so must be placed at **host** path `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so` (bind-mounted into syncd as `/usr/share/sonic/platform/libsai-stat-shim.so`)
- Shim only activates on syncd (re)start (LD_PRELOAD hooks at load time)
- Cache file shared via bind mount: host `/var/run/docker-syncd/` → container `/var/run/sswsyncd/`
- Daemon BCM config on host: `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm`
- bcmcmd socket on host: `/var/run/docker-syncd/sswsyncd.socket`
- **Bug fixed this session:** ps regex `(\S+)\((\d+)\)` → `(\S+)\(\s*(\d+)\)` to handle space-padded SDK port numbers in bcmcmd output (was only matching 32 of 128 ports)
