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

---

## Update: 2026-04-07 — DPB Counter Continuity (stage_25 complete)

The approach settled on Option A from above: the C daemon (`daemon.c`) handles all breakout ports, removes them from FlexCounter every cycle, and writes both COUNTERS and RATES to Redis DB2 directly. The SAI shim is no longer needed for the counter path.

Two new bugs were discovered and fixed during stage_25 test development.

### Bug 1: uint64 Underflow on BCM Counter Reset

**Trigger:** syncd restart (which happens during DPB) resets BCM SDK port counters to 0.

**Mechanism:**
```
last_in_oct = 40,000,000,000   (snapshot before syncd restart)
in_oct      =              0   (BCM counter reset to 0 after restart)
delta       = (uint64) 0 - 40,000,000,000
            = 18,446,744,033,709,551,616  (wraps to ~1.84e19)
rx_bps      = 1.84e19 / 3s = 456 TB/s    ← impossible
```

**Fix:** Before subtracting, compare: `in_oct >= last_in_oct ? delta/delta_s : 0.0`. Emit 0 for the reset cycle rather than an astronomic spike.

**Location:** `daemon.c` `write_rates()`, rate_state≥1 path.

### Bug 2: BCM Transient Zero During DPB → Rate Spike

This is the subtler and more interesting bug.

**Trigger:** Dynamic Port Breakout (changing Ethernet80 from 4×25G to 1×100G). Syncd restarts, causing:
1. All port OIDs change → daemon calls `refresh_flex_ports()` → sets `rate_state=0` for all affected ports (including Ethernet0, which shares nothing with Ethernet80 but gets a new OID)
2. BCM briefly reports `in_oct=0` for **unrelated** ports (xe87=Ethernet0) while it completes the port reconfiguration

**Why BCM returns 0:** During DPB, BCM internally restructures port objects. For a brief window (one 3-second poll cycle), `show c all xe87` returns 0 bytes even though ~40 GB have been received on that port.

**The spike mechanism, step by step:**

| Cycle | BCM `in_oct` | `rate_state` | Action | `last_in_oct` |
|-------|-------------|--------------|--------|----------------|
| Pre-DPB | 40,000,000,000 | 2 (EWMA active) | normal rate ~4 B/s | 39,999,999,988 |
| DPB fires | — | reset to **0** | OID changed, state cleared | — |
| Cycle T+0 | **0** (transient) | 0 → baseline capture | stores 0 as baseline | **0** |
| Cycle T+1 | 40,000,000,000 (real) | 1 → first rate | delta = 40 GB, Δt = 3s | — |
| Rate T+1 | — | — | **40 GB / 3s = 5934 MB/s** ← spike | — |

The underflow fix doesn't help here: `40 GB > 0` is TRUE (delta is positive), so the guard doesn't fire. The spike is real arithmetic, just from a bogus baseline.

**What "missed cycles" means:** The 40 GB did not accumulate in 3 seconds. It accumulated over the entire port uptime (hours). The 3-second window we used was incorrect — it spans only the post-transient interval, not the actual traffic duration. Attributing all historical bytes to one poll cycle produces a physically impossible rate.

**Fix (the cycle-count approach):** Add `int zero_cycles` to `flex_port_t`. In the `rate_state=0` baseline-capture path:

```c
if (in_oct == 0) {
    fp->zero_cycles++;
    fp->last_time = now;   /* extend window for when data returns */
    return;                /* DO NOT commit 0 as baseline */
}
/* first non-zero reading: safe to use as baseline */
fp->rate_state = 1;
fp->zero_cycles = 0;
```

The key property: we never commit the transient zero. Next cycle BCM returns the real 40 GB, which becomes the baseline. The cycle after THAT shows a small delta (actual traffic in 3 seconds), producing a correct rate.

**Why this is better than clamping:** A max-rate clamp would silently discard traffic that genuinely accumulated during a missed cycle. The zero-deferral approach is loss-free: we just delay the baseline by one cycle, then measure accurately from the first real reading.

### Bug 3: LED Daemon bcmcmd Socket Monopolisation

Not a counter bug, but it blocked the daemon from ever initialising.

**Problem:** `wedge100s-ledup-linkstate` (LED synchronisation daemon) maintained a **persistent** connection to `/var/run/docker-syncd/sswsyncd.socket`. The bcmcmd diag shell only accepts one session at a time. In steady state (no port link changes), the LED daemon held the socket open indefinitely doing nothing, blocking the flex-counter-daemon from connecting to run its initial `ps` map and subsequent counter polls.

**Evidence:** New flex-counter-daemon PIDs would log `bcmcmd banner timeout` every 3 seconds indefinitely. PID 1126823 (from a prior test run) succeeded because it started during a window where the LED daemon briefly dropped its connection on error.

**Fix:** The LED daemon now opens bcmcmd only when it has actual work to send (link-state changes or `.set` file overrides). In steady state it holds no socket:

```python
# OLD: one persistent BcmcmdClient for the daemon lifetime
with BcmcmdClient(...) as bcm:
    while True:
        time.sleep(1)
        _process_set_files(bcm, current)      # may or may not use bcm
        _apply_states(bcm, states, current)   # may or may not use bcm
        # socket held even when nothing sent

# NEW: connect only when there is work
while True:
    time.sleep(1)
    if not (has_set_files or changes):
        continue                              # socket released this cycle
    with BcmcmdClient(...) as bcm:            # connect → work → disconnect
        bcm._connect()
        _process_set_files(bcm, current)
        _apply_states(bcm, states, current)
```

This gives the flex-counter-daemon a window every poll cycle (3s) to connect, run `ps`, fetch counters, and disconnect — which takes well under 1 second.

### `rate_state` Machine Reference

| State | Meaning | Transitions |
|-------|---------|-------------|
| 0 | No baseline. OID just assigned or changed. | → stays 0 if BCM returns `in_oct=0` (transient); → 1 on first non-zero reading |
| 1 | Baseline captured. One full delta window needed. | → 2 on next non-zero poll (first rate computed) |
| 2 | EWMA active. Rates published every cycle. | → 0 if OID changes (syncd restart / DPB) |

### Test Sequence: `test_dpb_counter_continuity`

```
Phase 1 (baseline): assert all flex ports have sane rates (< 110% link speed)
Phase 2 (DPB):      config interface Ethernet80 breakout 1×100G
                    wait 9s (3 poll cycles)
Phase 3 (assert):   assert all flex ports (including Ethernet0–3) still sane
Phase 4 (restore):  config interface Ethernet80 breakout 4×25G
                    wait 9s
Phase 5 (assert):   assert all flex ports sane again
```

The 9-second wait after DPB allows:
- Cycle T+0: BCM transient zero → deferred (zero_cycles=1)
- Cycle T+3s: BCM real value → baseline captured (rate_state=1)
- Cycle T+6s: first delta → rates published (rate_state=2)
- Test asserts at T+9s: rates valid ✓

### Final Test Status (2026-04-07, verified on hardware)

| Test | Result |
|------|--------|
| test_flex_counter_daemon_running | ✅ PASS |
| test_flex_counter_daemon_bcm_config | ✅ PASS |
| test_flex_counter_daemon_ps_map | ✅ PASS |
| test_flex_ports_have_full_stats | ✅ PASS |
| test_non_flex_ports_not_regressed | ✅ PASS |
| test_flex_port_rx_bytes_nonzero | ✅ PASS |
| test_flex_port_tx_bytes_nonzero | ✅ PASS |
| test_startup_zeros_succeed | ✅ PASS |
| test_flex_ports_removed_from_flex_counter | ✅ PASS |
| test_flex_port_rates_sane | ✅ PASS |
| test_breakout_transition | ✅ PASS |
| test_nonbreakout_dpb_round_trip_retains_stats | ✅ PASS |
| test_sonic_clear_counters_flex_and_nonbreakout | ✅ PASS |
| test_counter_parity_via_iperf | ✅ PASS |
| test_dpb_counter_continuity | ✅ PASS |
| **stage_25_shim total** | **15/15** |

Commits: `d25c3d6c6` (uint64 underflow), `7e3bcbd55` (transient-zero deferral + LED daemon socket fix)
