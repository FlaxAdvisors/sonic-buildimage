# DPB Counter Continuity Test — Design Spec

**Date:** 2026-04-06  
**Stage:** `stage_25_shim/test_shim.py` (new function at end of file)  
**Motivation:** Today's incident — two daemons racing after a DPB event caused rates to
diverge to 87,162 MB/s on a 25G port. This test is the automated detector for that class
of failure.

---

## What We're Testing

The flex-counter daemon must survive a DPB event on an unrelated QSFP group without
corrupting counters on the remaining witness ports. Specifically:

1. Byte counters must be monotonically non-decreasing.
2. Rates must remain within physical link-speed limits.
3. The byte delta since baseline must be bounded by `link_speed × elapsed_time` — the
   physics ceiling. This catches explosion (daemon writing stale-accumulated or wrong-port
   values) as well as the EWMA divergence pattern.

---

## Ports

| Role | Ports | Mode | Why |
|------|-------|------|-----|
| Subject | Ethernet80–83 | 4×25G → 1×100G → 4×25G | DPB target; no 100G peer so link-down in native mode |
| Witnesses | Ethernet0, Ethernet1, Ethernet66, Ethernet67 | 4×25G / 4×10G | Must remain sane throughout; have link-up |

---

## Test Phases

### Phase 1 — Baseline
- Snapshot OIDs + `(IF_IN_OCTETS, IF_OUT_OCTETS)` for all 12 `FLEX_PORTS`.
- Record `phase1_time = time.time()`.
- Assert rates sane for all FLEX_PORTS (reuses `_assert_rates_sane`).
- 12 s sleep → assert witness bytes grew (daemon is writing, links are live).

### Phase 2 — DPB: Ethernet80 → 1×100G
- Snapshot witness byte counts (`pre_dpb_snap`).
- Execute `sudo config interface breakout Ethernet80 '1x100G[40G]' -y -f -l`.
- `_wait_for_oids(Ethernet80–83, present=False)` — sub-port OIDs disappear.
- `_wait_for_oids([Ethernet80], present=True)` — new 100G OID appears.
- Sleep 9 s (3 daemon cycles) for daemon to detect OID change and re-stabilize.
- Assert witness rates sane.
- Assert witness bytes monotone: `current >= pre_dpb_snap`.

### Phase 3 — Post-DPB Continuity (the key check)
- Immediately snapshot witnesses (`post_dpb_snap`).
- Compute `elapsed = time.time() - phase1_time`.
- **Physics bound:** `post_dpb_snap[port] - phase1_snap[port] <= speed_bps[port] × elapsed × 1.1`
  for each witness. Catches counter explosion and wrong-port mapping.
- **Monotone:** `post_dpb_snap[port] >= phase1_snap[port]` for each witness.
- Sleep 12 s → assert at least one witness still accumulating (proof of daemon liveness).

### Phase 4 — DPB Restore: Ethernet80 → 4×25G
- Snapshot witnesses (`pre_restore_snap`).
- Execute `sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l`.
- `_wait_for_oids(Ethernet80–83, present=True)` — sub-port OIDs reappear.
- Sleep 9 s for daemon to re-engage (detect new OIDs, remove from FlexCounter, begin writing).
- Assert ALL 12 FLEX_PORTS rates sane.
- Assert witness bytes monotone: `current >= pre_restore_snap`.

### Finally (always runs)
- If Ethernet80 not in `4x25G[10G]` mode, restore it.
- `_wait_for_oids(Ethernet80–83, present=True)`.
- Re-add Ethernet80–83 to VLAN 10 if DPB removed them.

---

## New Helpers

### `_get_speed_bps(ssh, port) -> float`
Reads `PORT|<port> speed` (Mbps) from CONFIG_DB.  
Returns `speed_mbps * 1e6 / 8` (bytes/sec ceiling).  
Falls back to 25G (3,125,000 B/s) if field absent.

### `_assert_rates_sane(ssh, ports)`
For each port in `ports`:
- Read `RATES:<oid> RX_BPS` and `TX_BPS`.
- `max_bps = _get_speed_bps(ssh, port)`.
- Assert `RX_BPS < max_bps * 1.1` and `TX_BPS < max_bps * 1.1`.

Reuses existing `_get_oid`.

### `_assert_bytes_monotone(before, after, ports)`
Pure dict comparison, no SSH.  
For each port: `assert after[port][IN] >= before[port][IN]` and same for OUT.

### `_assert_bytes_bounded(phase1_snap, current_snap, speed_bps_by_port, elapsed_s)`
For each port in intersection of both snaps:  
`delta = current_snap[port][IN] - phase1_snap[port][IN]`  
`assert delta <= speed_bps_by_port[port] * elapsed_s * 1.1`  
`assert delta >= 0`

### `_assert_bytes_growing(ssh, ports, wait_s=12)`
Take two snapshots `wait_s` apart.  
Assert at least one port in `ports` accumulated bytes (link-up proof).  
Skips if no ports are link-up (uses `show interfaces status`).

---

## Failure Messages

Each assertion includes a diagnostic message:
- Rate violation: `{port} RX_BPS={rate/1e6:.1f} MB/s exceeds {max_bps/1e6:.1f} MB/s ({speed}G link) — rate explosion`
- Monotone violation: `{port} IF_IN_OCTETS went backwards: {before} → {after}`
- Physics violation: `{port} delta {delta/1e6:.1f} MB in {elapsed:.0f}s exceeds {speed}G link capacity — counter corrupted`

---

## Constraints

- No iperf3 dependency — counter deltas during sleep windows prove liveness.
- Total wall time: ~60–90 s (two DPB operations × ~5 s each + three sleep windows).
- `finally` block always restores Ethernet80 and VLAN membership.
- Test is independent from `test_breakout_transition` (different subject port).
