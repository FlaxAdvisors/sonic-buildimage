# SAI Stat Shim — Hardware Verification Results

Verified on hardware 2026-03-28/29, SONiC hare-lorax, BCM56960 Tomahawk.
Counter accumulation fix verified 2026-03-29.

## Summary

The `libsai-stat-shim.so` LD_PRELOAD shim for flex sub-port counter collection is
fully deployed, stable, and functional.  bcmcmd integration works via the
`sswsyncd.socket` PTY proxy. **21/21 tests pass** (all stages 24 and 25).

## Test Results (2026-03-29, after counter accumulation fix + DPB + clear tests)

### stage_25_shim (11/11 pass)

| Test | Result | Notes |
|---|---|---|
| test_shim_library_present | PASS | libsai-stat-shim.so present in hwsku dir |
| test_syncd_sh_patched | PASS | /usr/bin/syncd.sh has LD_PRELOAD line |
| test_syncd_has_ld_preload | PASS | running syncd container shows LD_PRELOAD |
| test_flex_ports_have_full_stats | PASS | all 12 flex ports have ≥60 stat keys |
| test_non_flex_ports_not_regressed | PASS | 100G ports still have ≥60 stat keys |
| test_startup_zeros_succeed | PASS | SAI_PORT_STAT_IN_DROPPED_PKTS key present |
| test_flex_port_rx_bytes_nonzero | PASS | IF_IN_OCTETS accumulates LLDP background |
| test_flex_port_tx_bytes_nonzero | PASS | IF_OUT_OCTETS accumulates LLDP background |
| test_shim_breakout_transition | PASS | 4x25G→1x100G→4x25G: g_cache persists across DPB |
| test_nonbreakout_dpb_round_trip_retains_stats | PASS | 1x100G→4x25G→1x100G: BCM HW counter preserved |
| test_sonic_clear_counters_flex_and_nonbreakout | PASS | soft-clear works for flex and non-flex ports |

### stage_24_counters (10/10 pass)

| Test | Result | Notes |
|---|---|---|
| test_flex_counter_port_stat_enabled | PASS | |
| test_counters_port_name_map_all_ports | PASS | |
| test_counters_db_oid_has_stat_entries | PASS | |
| test_counters_key_fields_present | PASS | |
| test_show_interfaces_counters_exits_zero | PASS | |
| test_show_interfaces_counters_columns | PASS | |
| test_show_interfaces_counters_port_rows | PASS | |
| test_counters_link_up_ports_show_U | PASS | |
| test_counters_link_up_ports_have_rx_traffic | PASS | RX_OK non-zero on Ethernet16 (LLDP) |
| test_sonic_clear_counters | PASS | |

## Hardware Verified Facts (verified 2026-03-29)

### Shim Initialization
- Parsed 128 lane→sdk_port entries from th-wedge100s-32x-flex.config.bcm on every syncd start
- `shim: sai-stat-shim initialised (libsaibcm 14.3.x / BCM56960)`
- bcmcmd connected and parsed 128 ports from `ps` command via sswsyncd.socket

### bcmcmd Protocol (verified 2026-03-29)
- Socket: `/var/run/sswsyncd/sswsyncd.socket` (inside syncd container, served by `dsserve` pid 27)
- dsserve architecture: creates PTY master/slave pair; syncd runs with PTY slave as controlling
  terminal; `_tty2ds` thread forwards PTY output → socket client; `_ds2tty` forwards socket → PTY
- Protocol: connect → send `"\n"` to prod shell → read until `"drivshell>"` → send commands
- Critical: NO banner is sent on connect (unlike telnet); must send `\n` first to trigger prompt
- backlog=1; only one client at a time; connecting via socat while syncd is running disrupts the
  BCM SDK session and causes syncd to exit

### GLIBC Compatibility
- Build container: trixie (glibc 2.38), syncd Docker image: bookworm (glibc 2.36)
- GCC 13+ redirects sscanf → `__isoc23_sscanf` (GLIBC_2.38) in C23 mode
- Fix: `compat.c` provides local `__isoc23_sscanf` as Base symbol → max GLIBC dep = 2.34

### Memory Safety
- libsai.so maps `sai_port_api_t` struct in `r--p` (read-only) memory
- Fix: `patch_fnptr()` using mprotect(PROT_READ|PROT_WRITE) before write, restore after
- Recursion guard: check `if (port_api->get_port_stats != shim_get_port_stats)` before saving

### Operational Notes
- Restart procedure: `docker stop syncd && docker rm syncd && systemctl restart swss`
  (must restart SWSS, not just syncd — syncd watches swss; independent restart fails with
  `processSwitches: SAI_STATUS_FAILURE` during INIT_VIEW)
- Old syncd container reuses stale LD_PRELOAD; `docker rm` forces fresh container with new env
- GLIBC compat: shim works inside bookworm syncd container (max dependency glibc 2.34)

## Counter Accumulation Root Cause (found 2026-03-29)

`bcmcmd 'show counters'` returns **per-call delta values**, not absolute cumulative totals.
The BCM diag shell accumulates hardware counters between calls and emits the increment since
the previous call.  Consequence: shim counters in COUNTERS_DB went to 0 after each 500ms
poll cycle whenever no new traffic occurred in that window.

**Fix in `parse_counters()` (`bcmcmd_client.c`)**:
- Removed `cache->n_rows = 0` (which destroyed accumulated values on each call)
- Added: reset `n_raw = 0` on all existing rows so `raw[]` holds current-call deltas for the
  name2 second-pass, while `val[]` continues to accumulate via `+=` across calls
- Increased `READ_BUF_SIZE` from 65536 → 262144 to handle heavier load output

Result: `val[]` entries now grow monotonically (running total since syncd start), matching
what SONiC COUNTERS_DB consumers expect.  Even LLDP background traffic (~3-12 KB/s per port)
is now visible in IF_IN/OUT_OCTETS within 1-2 seconds after syncd initialization.

## Files Changed

- `wedge100s-32x/sai-stat-shim/Makefile` — added compat.o to OBJS
- `wedge100s-32x/sai-stat-shim/compat.c` — NEW: provides __isoc23_sscanf locally
- `wedge100s-32x/sai-stat-shim/shim.c` — mprotect patch_fnptr(), recursion guard, sys/mman.h
- `wedge100s-32x/sai-stat-shim/bcmcmd_client.c` — send `\n` before reading banner; diagnostic
  logging; delta accumulation fix (no n_rows reset); READ_BUF_SIZE 65536→262144
- `debian/rules` — build and install libsai-stat-shim.so (already present)
- `tests/stage_23_throughput/test_throughput.py` — added round1_reverse and round2_reverse
  tests to validate OUT counters on ports that are servers in rounds 1/2
- `tests/stage_24_counters/test_counters.py` — fixed stale docstring in
  test_counters_key_fields_present (pre-shim comment removed)
- `tests/stage_25_shim/test_shim.py` — added 3 new tests:
  - `test_shim_breakout_transition`: Ethernet0 4x25G→1x100G→4x25G; verifies g_cache
    persists across DPB (counters restored on restore), bcmcmd reconnects each DPB
  - `test_nonbreakout_dpb_round_trip_retains_stats`: Ethernet16 1x100G→4x25G→1x100G;
    verifies BCM hardware counter ≥ baseline (HW register never resets on DPB)
  - `test_sonic_clear_counters_flex_and_nonbreakout`: verifies sonic-clear is a soft
    display-layer clear (portstat resets, COUNTERS_DB unchanged) for both flex and
    non-flex ports

## Counter Behavior Across DPB (verified 2026-03-29)

### Flex port DPB round-trip (4x25G → 1x100G → 4x25G)
- New sub-port OIDs are created on each DPB (COUNTERS_PORT_NAME_MAP entries change)
- Counters appear **immediately non-zero** after restore because `g_cache.val[]` persists
  across `sai_api_query` calls (g_cache is never reset, only g_oids is cleared)
- `g_oids` is rebuilt from scratch after each DPB; the flex/non-flex classification
  re-runs on the first `get_port_stats` call for each new OID
- The 1x100G Ethernet0 shows 0 counters — expected (no 100G peer; BCM HW port has
  no traffic and returns all-zero bcmcmd 'show counters' deltas)

### Non-flex port DPB round-trip (1x100G → 4x25G → 1x100G)
- Counters after restore are ≥ baseline because BCM hardware counter registers are
  **never reset on DPB** — they run continuously regardless of SAI port object lifecycle
- The brief 4x25G period shows 0 counters on sub-ports (no 25G peer for Ethernet16)

### sonic-clear counters (SONiC counter reset command, verified 2026-03-29)
- **Command**: `sonic-clear counters` (calls `portstat -c`)
- **Effect**: saves JSON snapshot of current COUNTERS_DB values to `/tmp/cache/portstat/<uid>/portstat`
- **NOT reset**: COUNTERS_DB values, shim `g_cache`, BCM hardware registers
- **IS reset**: portstat display — subsequent `portstat` / `show interfaces counters` shows
  delta since the snapshot (appears near-zero immediately after clear)
- Works identically for flex and non-flex ports (operates on COUNTERS_DB regardless of source)
- No per-port clear option: `portstat -c -i Ethernet0` saves ALL ports as baseline,
  `-i` only filters the display output
- After clear, portstat shows new traffic in 500ms-1s (next flex counter poll cycle)

## Known Limitations

1. **Single bcmcmd session**: dsserve backlog=1, one client at a time. The shim holds the
   connection open for the syncd process lifetime. No concurrent bcmcmd clients possible.

2. **Startup race**: bcmcmd connection retried every 2 seconds. First successful connect
   typically happens ~14 seconds after syncd starts (after BCM SDK fully initializes).
   Counters return 0 until bcmcmd connects (this is expected and not a test failure).
