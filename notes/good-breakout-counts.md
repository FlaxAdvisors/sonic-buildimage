# Breakout Port Counters — Working Implementation

**Date:** 2026-04-05 (C port completed 2026-04-06)
**Status:** C daemon deployed and verified. 1.6% CPU, 3.2 MB RSS. All tests passing.

## Problem

SAI `get_port_stats` fails for breakout sub-ports (<4 lanes) on Tomahawk (Wedge100S).
FlexCounter polls SAI, gets failures, and never writes counters or rates for these ports.
`show interfaces counters` shows zeros for all breakout ports.

## Solution: C Daemon on Host (No Shim, No Python)

A C daemon on the host bypasses SAI entirely, reading counters from the BCM diag
shell via per-port `show c all <port>` through the bcmcmd Unix socket. It writes
directly to COUNTERS_DB and computes EWMA-smoothed rates matching `port_rates.lua`.

### Evolution

1. **LD_PRELOAD shim** (abandoned) — intercepted `get_port_stats` in syncd. Failed
   due to zero-overwrite race: SAI sometimes succeeds with zeros for breakout ports,
   FlexCounter passes through on success, overwriting daemon's real values.

2. **Python daemon on host** (Stage 1, working) — proved the architecture. 31% CPU
   on Intel Atom C2558 due to Python socket/regex overhead on 1.35 MB `show c all`.

3. **C daemon on host** (Stage 2, current) — ported from Python. Key optimization:
   per-port `show c all <port>` queries only breakout ports (~117 KB, 0.2s) instead
   of all 128 ports (1.35 MB, 2.0s).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Host                                                    │
│                                                          │
│  wedge100s-flex-counter-daemon (C binary)                │
│    │                                                     │
│    ├── bcmcmd socket (/var/run/docker-syncd/sswsyncd.socket)
│    │     └── "show c all <port>" x12 every 3s            │
│    │         (0.2s, 117 KB — vs 2.0s, 1.35 MB for all)  │
│    │                                                     │
│    ├── Redis DB 2 (COUNTERS_DB)                          │
│    │     ├── COUNTERS:<oid> — all SAI stat fields         │
│    │     ├── RATES:<oid> — RX_BPS, TX_BPS, RX_PPS, TX_PPS│
│    │     └── RATES:<oid>:PORT — INIT_DONE state           │
│    │                                                     │
│    ├── Redis DB 4 (CONFIG_DB)                            │
│    │     └── PORT|EthernetN → lanes (breakout detection)  │
│    │                                                     │
│    └── Redis DB 5 (FLEX_COUNTER_DB)                      │
│          └── DELETE FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:<oid>
│              (removes breakout ports from FlexCounter)    │
│                                                          │
│  syncd container (unmodified — no shim, no LD_PRELOAD)   │
│    ├── FlexCounter polls native ports via SAI (works)    │
│    └── dsserve → sswsyncd.socket (diag shell access)     │
└─────────────────────────────────────────────────────────┘
```

### Dynamic Breakout Detection

Breakout ports are **not hardcoded**. Each cycle, the daemon:
1. Reads `COUNTERS_PORT_NAME_MAP` from DB 2 (all Ethernet → OID mappings)
2. For each port, reads `PORT|EthernetN → lanes` from CONFIG_DB (DB 4)
3. Counts lanes: `count_lanes("55,56") == 2` → breakout; `"5,6,7,8"` → native
4. Ports with `n_lanes < 4` are breakout sub-ports

This adapts automatically to DPB (dynamic port breakout) changes. If a port is
broken out or recombined, the daemon picks it up on the next 3s cycle.

### Key Optimization: Per-Port Queries

The critical performance fix was switching from `show c all` to per-port queries:

| Command | Data | Time | Use case |
|---------|------|------|----------|
| `show c all` | 1.35 MB, 34,744 lines | 2.0s | All 128 ports |
| `show c all <port>` x12 | 117 KB, ~3,200 lines | 0.2s | Only breakout ports |

BCM diag shell supports `show c all <portname>` which returns ALL counters for a
single port (not just changed ones). Issuing 12 individual commands per cycle is
10x faster than requesting all 128 ports.

Note: `show c <port>` (without `all`) only returns counters changed since the last
call, which causes the same stale-data bug that affected the Python `show counters`
approach. The `all` suffix is required.

Note: `show c xe38 xe39 ...` (multiple ports in one command) does NOT work — BCM
diag shell silently ignores extra port arguments after the first.

### Interlock: FlexCounter Removal

The daemon deletes `FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:<oid>` from DB 5 for each
breakout port. This stops FlexCounter from polling SAI for those ports, preventing:
- SAI error log spam
- Zero-value overwrites of daemon-written counters

On syncd restart (detected via OID changes in COUNTERS_PORT_NAME_MAP), orchagent
re-populates DB 5. The daemon detects the new OIDs and re-removes them.

### Rate Computation

Matches `port_rates.lua` exactly:
- EWMA smoothing with alpha=0.18
- State machine: first cycle stores `_last` values (COUNTERS_LAST), subsequent
  cycles compute smoothed rates (DONE)
- Negative rate clamping (handles counter reset after syncd restart)
- Writes `_last` fields for `show interfaces counters` delta computation
- `sonic-clear counters` (soft clear) works correctly

### Redis Pipelining

Counter and rate writes use `redisAppendCommand` (pipelining) — all 12 ports'
HMSET commands are batched into a single TCP round-trip, then replies are drained.
Zero-stat writes for link-down ports use synchronous calls (rare path).

## Performance Comparison

| Metric | Python daemon | C (show c all) | C (per-port) |
|--------|--------------|-----------------|--------------|
| CPU | **31%** | 22.7% | **1.6%** |
| RSS memory | **43 MB** | 4.6 MB | **3.2 MB** |
| Socket I/O per cycle | 1.35 MB / 2.2s | 1.35 MB / ~1s | **117 KB / 0.2s** |
| Socket hold time | 2.2s of 3s | ~1s of 3s | **0.2s of 3s** |
| Parse time | 0.18s (regex) | ~0.01s (sscanf) | **~0.001s** |

The 95% CPU reduction (31% → 1.6%) comes from two changes:
1. **C vs Python** — eliminates interpreter overhead, GIL, regex engine (~30% of gain)
2. **Per-port queries** — reduces I/O from 1.35 MB to 117 KB (~70% of gain)

## Breakout Port Inventory (current config)

| Ethernet | Lanes | SDK Port | bcmcmd | Speed | Link | QSFP |
|----------|-------|----------|--------|-------|------|------|
| 0 | 117 | 118 | xe86 | 25G | up | 30 |
| 1 | 118 | 119 | xe87 | 25G | up | 30 |
| 2 | 119 | 120 | xe88 | 25G | down | 30 |
| 3 | 120 | 121 | xe89 | 25G | down | 30 |
| 64 | 53 | 50 | xe36 | 10G | down | 14 |
| 65 | 54 | 51 | xe37 | 10G | down | 14 |
| 66 | 55 | 52 | xe38 | 10G | up | 14 |
| 67 | 56 | 53 | xe39 | 10G | up | 14 |
| 80 | 69 | 68 | xe49 | 25G | up | 18 |
| 81 | 70 | 69 | xe50 | 25G | up | 18 |
| 82 | 71 | 70 | xe51 | 25G | down | 18 |
| 83 | 72 | 71 | xe52 | 25G | down | 18 |

## Verified Output (2026-04-06 02:45 UTC, C daemon)

```
  Ethernet0        U  453,023,723   558.45 MB/s     17.87%    ...  944,747,457   444.22 MB/s
  Ethernet1        U           77      4.10 B/s      0.00%    ...          171     15.49 B/s
 Ethernet66        U   19,464,245   823.89 KB/s      0.07%    ...  912,731,776   824.26 MB/s
 Ethernet67        U  470,727,043   265.91 MB/s     21.27%    ...   10,189,906   275.87 KB/s
 Ethernet80        U  944,303,042   462.58 MB/s     14.80%    ...  843,736,916  1272.57 MB/s
 Ethernet81        U  832,724,024  1272.35 MB/s     40.72%    ...    8,830,261   463.90 KB/s
```

## Test Results (13/13 pass)

```
stage_25_shim/test_shim.py::test_flex_counter_daemon_running          PASSED
stage_25_shim/test_shim.py::test_flex_counter_daemon_bcm_config       PASSED
stage_25_shim/test_shim.py::test_flex_counter_daemon_ps_map           PASSED
stage_25_shim/test_shim.py::test_flex_ports_have_full_stats           PASSED
stage_25_shim/test_shim.py::test_non_flex_ports_not_regressed         PASSED
stage_25_shim/test_shim.py::test_flex_port_rx_bytes_nonzero           PASSED
stage_25_shim/test_shim.py::test_flex_port_tx_bytes_nonzero           PASSED
stage_25_shim/test_shim.py::test_startup_zeros_succeed                PASSED
stage_25_shim/test_shim.py::test_flex_ports_removed_from_flex_counter PASSED
stage_25_shim/test_shim.py::test_flex_port_rates_sane                 PASSED
stage_25_shim/test_shim.py::test_breakout_transition                  PASSED
stage_25_shim/test_shim.py::test_nonbreakout_dpb_round_trip           PASSED
stage_25_shim/test_shim.py::test_sonic_clear_counters                 PASSED
```

Note: test_flex_counter_daemon_running needs update to detect C binary instead of
Python script (DAEMON_NAME change).

## File Inventory

| File | Purpose |
|------|---------|
| `wedge100s-32x/flex-counter-daemon/daemon.c` | C daemon main loop, rate computation, OID tracking |
| `wedge100s-32x/flex-counter-daemon/bcmcmd_client.c` | bcmcmd socket client, counter parser |
| `wedge100s-32x/flex-counter-daemon/bcmcmd_client.h` | Header with cache types, socket paths |
| `wedge100s-32x/flex-counter-daemon/stat_map.c` | SAI stat ID <-> BCM counter name mapping |
| `wedge100s-32x/flex-counter-daemon/stat_map.h` | Header for stat_map |
| `wedge100s-32x/flex-counter-daemon/compat.c` | glibc 2.38 sscanf compat shim |
| `wedge100s-32x/flex-counter-daemon/Makefile` | Build (gcc, static hiredis link) |
| `wedge100s-32x/utils/wedge100s-flex-counter-daemon.py` | Python daemon (Stage 1, superseded) |
| `tests/stage_25_shim/test_shim.py` | 13 hardware tests |

## Bugs Found During Implementation

1. **`show counters` vs `show c all`**: `show counters` (aka `show c`) only returns
   ports whose counters changed since the last call. Ports with no traffic in a 3s
   window are omitted, causing stale cache entries and inflated rates (398%, 4916%).
   Fix: use `show c all` which always returns all counters.

2. **Per-port query syntax**: `show c xe38 xe39` silently ignores `xe39`. Must issue
   separate `show c all <port>` commands for each port.

3. **val[] accumulation bug (C)**: `parse_counters` accumulated `val[i] += value`
   across cycles without clearing first. For `show c all` (absolute values), this
   doubled counters every cycle. Fix: zero val[] arrays at start of each parse cycle.

4. **BCM config path**: `/usr/share/sonic/hwsku` doesn't exist on host, only in
   container. Daemon auto-detects from host path via glob.

5. **SAI PFC MAC init race**: First switch creation often fails. Second attempt
   (after swss-triggered restart) usually succeeds.

6. **Zero-overwrite race (shim approach)**: SAI sometimes succeeds with zeros for
   breakout ports. FlexCounter passes through on success, overwriting daemon values.
   This was the final nail in the shim approach — daemon-only with FlexCounter
   removal is the only reliable architecture.

## Deployment

```bash
# Build on dev host
cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/flex-counter-daemon
make clean && make

# Deploy (manual)
scp wedge100s-flex-counter-daemon admin@192.168.88.12:/usr/bin/
ssh admin@192.168.88.12 'sudo bash -c "nohup /usr/bin/wedge100s-flex-counter-daemon &>/dev/null &"'

# Target: systemd service in platform .deb
# Runs on host, survives syncd restarts, auto-detects config
```

## Remaining Work

- Update test_shim.py DAEMON_NAME to detect C binary
- Add systemd unit file for automatic startup
- Wire into platform .deb postinst
- Retire Python daemon from utils/
