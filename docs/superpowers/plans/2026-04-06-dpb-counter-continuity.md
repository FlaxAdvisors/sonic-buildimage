# DPB Counter Continuity Test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `test_dpb_counter_continuity` to `tests/stage_25_shim/test_shim.py` — an automated detector for the counter-explosion / EWMA-divergence failure class triggered by a DPB event on an unrelated QSFP group.

**Architecture:** Five helper functions are inserted into the existing helpers block (after `_wait_for_oids`); the test function is appended at the end of the file. All helpers are pure-Python or use the existing `ssh.run` / `_get_oid` / `_counters_snapshot` patterns. No new imports needed — `time` is already imported.

**Tech Stack:** pytest, redis-cli (via ssh), SONiC CONFIG_DB / COUNTERS_DB, `config interface breakout` CLI

---

## File Map

| Action | Path | What changes |
|--------|------|--------------|
| Modify | `tests/stage_25_shim/test_shim.py` | Insert 5 helpers after line ~312 (after `_wait_for_oids`); append test function at EOF |

No new files. Everything goes into the single existing test module.

---

## Task 1: Insert helper functions into the helpers block

**Files:**
- Modify: `tests/stage_25_shim/test_shim.py` (after the blank line following `_wait_for_oids`, before `# DPB transition test`)

- [ ] **Step 1: Verify insert point**

  Run:
  ```bash
  grep -n "_wait_for_oids\|DPB transition" tests/stage_25_shim/test_shim.py | head -10
  ```
  Expected: `_wait_for_oids` ends around line 311, the `# DPB transition test` banner is around line 314.

- [ ] **Step 2: Insert the five helpers**

  In `tests/stage_25_shim/test_shim.py`, locate the block:
  ```python
  # ---------------------------------------------------------------------------
  # DPB transition test
  # ---------------------------------------------------------------------------
  ```
  Insert the following **immediately before** that banner (after the blank line ending `_wait_for_oids`):

  ```python
  # ---------------------------------------------------------------------------
  # DPB counter-continuity helpers
  # ---------------------------------------------------------------------------

  def _get_speed_bps(ssh, port):
      """Return byte-rate ceiling for port from CONFIG_DB (bytes/sec).

      Reads PORT|<port> speed (Mbps).  Falls back to 25G if absent.
      """
      out, _, _ = ssh.run(f"redis-cli -n 4 hget 'PORT|{port}' speed", timeout=10)
      try:
          speed_mbps = int(out.strip())
      except (ValueError, AttributeError):
          speed_mbps = 25000  # 25G fallback
      return speed_mbps * 1e6 / 8   # convert to bytes/sec


  def _assert_rates_sane(ssh, ports):
      """Assert each port's RX_BPS and TX_BPS are within 110% of link speed.

      Uses RATES:<oid> in COUNTERS_DB (DB 2).  Skips ports with no OID.
      """
      violations = []
      for port in ports:
          oid = _get_oid(ssh, port)
          if not oid:
              continue
          max_bps = _get_speed_bps(ssh, port)
          for direction in ["RX_BPS", "TX_BPS"]:
              val_out, _, _ = ssh.run(
                  f"redis-cli -n 2 hget 'RATES:{oid}' {direction}", timeout=10
              )
              rate = float(val_out.strip() or "0")
              if rate > max_bps * 1.1:
                  speed_g = int(max_bps * 8 / 1e9)
                  violations.append(
                      f"{port} {direction}={rate/1e6:.1f} MB/s exceeds "
                      f"{max_bps/1e6:.1f} MB/s ({speed_g}G link) — rate explosion"
                  )
      assert not violations, (
          "Rate sanity check failed:\n" + "\n".join(f"  {v}" for v in violations)
      )


  def _assert_bytes_monotone(before, after, ports):
      """Assert IN and OUT octets did not decrease for each port in ports.

      before, after: {port: (in_octets, out_octets)} dicts (from _counters_snapshot).
      """
      violations = []
      for port in ports:
          if port not in before or port not in after:
              continue
          b_in, b_out = before[port]
          a_in, a_out = after[port]
          if a_in < b_in:
              violations.append(
                  f"{port} IF_IN_OCTETS went backwards: {b_in:,} → {a_in:,}"
              )
          if a_out < b_out:
              violations.append(
                  f"{port} IF_OUT_OCTETS went backwards: {b_out:,} → {a_out:,}"
              )
      assert not violations, (
          "Byte counter monotonicity violated:\n"
          + "\n".join(f"  {v}" for v in violations)
      )


  def _assert_bytes_bounded(phase1_snap, current_snap, speed_bps_by_port, elapsed_s):
      """Assert counter deltas do not exceed physics ceiling since phase1.

      phase1_snap, current_snap: {port: (in_octets, out_octets)}.
      speed_bps_by_port: {port: bytes/sec ceiling}.
      elapsed_s: seconds since phase1_snap was taken.

      Catches counter explosion (87 MB/s on a 25G link) and wrong-port mapping.
      """
      violations = []
      for port in set(phase1_snap) & set(current_snap):
          ceiling = speed_bps_by_port.get(port, 3_125_000) * elapsed_s * 1.1
          for idx, direction in enumerate(["IF_IN_OCTETS", "IF_OUT_OCTETS"]):
              delta = current_snap[port][idx] - phase1_snap[port][idx]
              if delta < 0:
                  violations.append(
                      f"{port} {direction} went backwards: "
                      f"{phase1_snap[port][idx]:,} → {current_snap[port][idx]:,}"
                  )
              elif delta > ceiling:
                  speed_g = int(speed_bps_by_port.get(port, 3_125_000) * 8 / 1e9)
                  violations.append(
                      f"{port} delta {delta/1e6:.1f} MB in {elapsed_s:.0f}s "
                      f"exceeds {speed_g}G link capacity — counter corrupted"
                  )
      assert not violations, (
          "Physics bound violated (counter explosion detected):\n"
          + "\n".join(f"  {v}" for v in violations)
      )


  def _assert_bytes_growing(ssh, ports, wait_s=12):
      """Assert at least one port in ports accumulated bytes over wait_s seconds.

      Skips the check if no port in ports is currently link-up (avoids false fails
      when all witnesses are link-down).
      """
      out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
      up_ports = [p for p in ports
                  if any(p in line and " up " in line for line in out.splitlines())]
      if not up_ports:
          print(f"  _assert_bytes_growing: no link-up ports in {ports} — skipping")
          return

      snap1 = _counters_snapshot(ssh, up_ports)
      time.sleep(wait_s)
      snap2 = _counters_snapshot(ssh, up_ports)

      grew = [p for p in up_ports
              if p in snap1 and p in snap2
              and (snap2[p][0] > snap1[p][0] or snap2[p][1] > snap1[p][1])]
      assert grew, (
          f"No port in {up_ports} accumulated bytes over {wait_s}s — "
          "daemon is not writing (check wedge100s-flex-counter-daemon status)"
      )
      print(f"  Bytes growing on: {grew} ✓")

  ```

- [ ] **Step 3: Syntax check**

  Run:
  ```bash
  python3 -m py_compile tests/stage_25_shim/test_shim.py && echo OK
  ```
  Expected output: `OK` (no tracebacks)

- [ ] **Step 4: Commit helpers**

  ```bash
  git add tests/stage_25_shim/test_shim.py
  git commit -m "test(stage_25): add DPB counter-continuity helper functions"
  ```

---

## Task 2: Append `test_dpb_counter_continuity` test function

**Files:**
- Modify: `tests/stage_25_shim/test_shim.py` (append after the last line of `test_counter_parity_via_iperf`)

- [ ] **Step 1: Append the test function**

  Add the following at the **end of the file** (after line 964 `print("Counter parity verified...")`):

  ```python


  # ---------------------------------------------------------------------------
  # DPB counter-continuity test
  # ---------------------------------------------------------------------------

  def test_dpb_counter_continuity(ssh):
      """Witness-port counters survive a DPB event on an unrelated QSFP group.

      A DPB on Ethernet80-83 (QSFP group, no 100G peer → link-down in 1x100G mode)
      must not corrupt counters on witness ports Ethernet0/1/66/67 that have live
      links.  The test verifies:
        1. Byte counters are monotonically non-decreasing throughout all phases.
        2. Rates remain within physical link-speed limits at all checkpoints.
        3. Total byte delta since baseline is bounded by link_speed × elapsed_time
           (detects the 87 MB/s-on-25G counter explosion / EWMA divergence pattern).

      Subject:   Ethernet80-83  (4x25G → 1x100G → 4x25G)
      Witnesses: Ethernet0, Ethernet1, Ethernet66, Ethernet67  (link-up)
      """
      DPB_PARENT  = "Ethernet80"
      DPB_PORTS   = ["Ethernet80", "Ethernet81", "Ethernet82", "Ethernet83"]
      WITNESSES   = ["Ethernet0", "Ethernet1", "Ethernet66", "Ethernet67"]
      DPB_TIMEOUT = 20

      # Pre-compute byte-rate ceilings for witnesses (CONFIG_DB speed field)
      speed_bps_by_port = {p: _get_speed_bps(ssh, p) for p in WITNESSES}

      # ------------------------------------------------------------------
      # Phase 1: baseline — confirm daemon is active on all 12 FLEX_PORTS
      # ------------------------------------------------------------------
      print("\n=== Phase 1: baseline ===")

      phase1_oids = {p: _get_oid(ssh, p) for p in FLEX_PORTS}
      for port in FLEX_PORTS:
          assert phase1_oids[port], (
              f"{port} has no OID in COUNTERS_PORT_NAME_MAP. "
              "Switch must be in standard breakout config (run tools/deploy.py)."
          )

      phase1_snap = _counters_snapshot(ssh, FLEX_PORTS)
      phase1_time = time.time()

      _assert_rates_sane(ssh, FLEX_PORTS)
      print("  Baseline rates sane for all 12 FLEX_PORTS ✓")

      # 12s sleep: at least one witness must accumulate bytes (daemon-liveness proof)
      _assert_bytes_growing(ssh, WITNESSES, wait_s=12)

      try:
          # ------------------------------------------------------------------
          # Phase 2: DPB Ethernet80 → 1x100G[40G]
          # ------------------------------------------------------------------
          print("\n=== Phase 2: DPB Ethernet80 → 1x100G[40G] ===")
          pre_dpb_snap = _counters_snapshot(ssh, WITNESSES)

          out, err, rc = ssh.run(
              f"sudo config interface breakout {DPB_PARENT} '1x100G[40G]' -y -f -l",
              timeout=30,
          )
          assert rc == 0, (
              f"DPB to 1x100G failed (rc={rc}): {err.strip()[:300]}\n{out.strip()[:300]}"
          )

          # Sub-port OIDs Ethernet81-83 must disappear from COUNTERS_PORT_NAME_MAP
          _wait_for_oids(ssh, ["Ethernet81", "Ethernet82", "Ethernet83"],
                         present=False, timeout_s=DPB_TIMEOUT)
          # New 1x100G OID for Ethernet80 must appear
          _wait_for_oids(ssh, [DPB_PARENT], present=True, timeout_s=DPB_TIMEOUT)

          # Allow 3 daemon cycles (~3s each) to detect OID change and re-stabilize
          print("  Waiting 9s for daemon to re-stabilize after DPB...")
          time.sleep(9)

          _assert_rates_sane(ssh, WITNESSES)
          print("  Post-DPB witness rates sane ✓")

          post_dpb_snap = _counters_snapshot(ssh, WITNESSES)
          _assert_bytes_monotone(pre_dpb_snap, post_dpb_snap, WITNESSES)
          print("  Post-DPB witness bytes monotone ✓")

          # ------------------------------------------------------------------
          # Phase 3: post-DPB continuity (the key check)
          # ------------------------------------------------------------------
          print("\n=== Phase 3: physics-bound continuity check ===")
          elapsed = time.time() - phase1_time
          _assert_bytes_bounded(phase1_snap, post_dpb_snap, speed_bps_by_port, elapsed)
          print(f"  Physics bound satisfied ({elapsed:.0f}s since baseline) ✓")

          # Daemon still writing after DPB
          _assert_bytes_growing(ssh, WITNESSES, wait_s=12)

          # ------------------------------------------------------------------
          # Phase 4: DPB Ethernet80 → 4x25G[10G] (restore)
          # ------------------------------------------------------------------
          print("\n=== Phase 4: DPB Ethernet80 → 4x25G[10G] (restore) ===")
          pre_restore_snap = _counters_snapshot(ssh, WITNESSES)

          out, err, rc = ssh.run(
              f"sudo config interface breakout {DPB_PARENT} '4x25G[10G]' -y -f -l",
              timeout=30,
          )
          assert rc == 0, (
              f"DPB restore to 4x25G failed (rc={rc}): {err.strip()[:300]}\n{out.strip()[:300]}"
          )

          _wait_for_oids(ssh, DPB_PORTS, present=True, timeout_s=DPB_TIMEOUT)

          # Allow daemon to detect new OIDs, remove from FlexCounter, begin writing
          print("  Waiting 9s for daemon to re-engage after restore...")
          time.sleep(9)

          # All 12 FLEX_PORTS must be sane after full round-trip
          _assert_rates_sane(ssh, FLEX_PORTS)
          print("  All 12 FLEX_PORTS rates sane after restore ✓")

          post_restore_snap = _counters_snapshot(ssh, WITNESSES)
          _assert_bytes_monotone(pre_restore_snap, post_restore_snap, WITNESSES)
          print("  Post-restore witness bytes monotone ✓")

          print("\n=== DPB counter continuity: PASS ===")

      finally:
          # Always restore Ethernet80 to 4x25G[10G] regardless of test outcome
          mode_out, _, _ = ssh.run(
              f"redis-cli -n 4 hget 'BREAKOUT_CFG|{DPB_PARENT}' brkout_mode",
              timeout=10,
          )
          if mode_out.strip() != "4x25G[10G]":
              print(f"\n  [cleanup] Restoring {DPB_PARENT} to 4x25G[10G]...")
              ssh.run(
                  f"sudo config interface breakout {DPB_PARENT} '4x25G[10G]' -y -f -l",
                  timeout=30,
              )
              _wait_for_oids(ssh, DPB_PORTS, present=True, timeout_s=30)
              print(f"  [cleanup] {DPB_PARENT} restored to 4x25G[10G]")

          # DPB removes sub-ports from VLAN 10; re-add them so subsequent tests
          # (sonic-clear counters, iperf parity) have L2 connectivity.
          for sp in DPB_PORTS:
              vlan_check, _, _ = ssh.run(
                  f"redis-cli -n 4 exists 'VLAN_MEMBER|Vlan10|{sp}'", timeout=5
              )
              if vlan_check.strip() != "1":
                  ssh.run(f"sudo config vlan member add 10 {sp}", timeout=10)
                  print(f"  [cleanup] Re-added {sp} to VLAN 10")
  ```

- [ ] **Step 2: Syntax check**

  ```bash
  python3 -m py_compile tests/stage_25_shim/test_shim.py && echo OK
  ```
  Expected: `OK`

- [ ] **Step 3: Commit the test function**

  ```bash
  git add tests/stage_25_shim/test_shim.py
  git commit -m "test(stage_25): add test_dpb_counter_continuity — detects counter explosion after DPB race"
  ```

---

## Task 3: Hardware verification

**Files:** none changed — this task runs the test against the live switch.

- [ ] **Step 1: Confirm hardware prerequisites**

  ```bash
  ssh admin@192.168.88.12 "show interfaces status | grep -E 'Ethernet(0|1|66|67|80|81|82|83)'"
  ```
  Expected: Ethernet0/1/66/67 show `up`; Ethernet80-83 show current 4x25G breakout mode (may be up or down).

- [ ] **Step 2: Confirm Ethernet80 is in 4x25G[10G] before running**

  ```bash
  ssh admin@192.168.88.12 "redis-cli -n 4 hget 'BREAKOUT_CFG|Ethernet80' brkout_mode"
  ```
  Expected: `4x25G[10G]`

  If not, restore it:
  ```bash
  ssh admin@192.168.88.12 "sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l"
  ```

- [ ] **Step 3: Run the test**

  ```bash
  cd tests && pytest stage_25_shim/test_shim.py::test_dpb_counter_continuity -v -s 2>&1 | tee ../notes/test-dpb-counter-continuity-run.txt
  ```
  Expected: `PASSED` (wall time ~65–90 s).

  Key output lines to look for:
  ```
  === Phase 1: baseline ===
    Baseline rates sane for all 12 FLEX_PORTS ✓
    Bytes growing on: ['Ethernet0', 'Ethernet1', ...] ✓
  === Phase 2: DPB Ethernet80 → 1x100G[40G] ===
    Post-DPB witness rates sane ✓
    Post-DPB witness bytes monotone ✓
  === Phase 3: physics-bound continuity check ===
    Physics bound satisfied (XX s since baseline) ✓
  === Phase 4: DPB Ethernet80 → 4x25G[10G] (restore) ===
    All 12 FLEX_PORTS rates sane after restore ✓
    Post-restore witness bytes monotone ✓
  === DPB counter continuity: PASS ===
  ```

- [ ] **Step 4: Write findings note**

  Save hardware run output to `notes/test-dpb-counter-continuity-run.txt` (already done by tee in step 3).

- [ ] **Step 5: Final commit if any fixups were needed**

  Only if step 3 required any code corrections:
  ```bash
  git add tests/stage_25_shim/test_shim.py
  git commit -m "fix(stage_25): fixup dpb_counter_continuity after hardware run"
  ```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by |
|---|---|
| Phase 1: snapshot all 12 FLEX_PORTS, record phase1_time, assert rates sane, 12s sleep, witness bytes grew | Task 2 Phase 1 |
| Phase 2: pre_dpb_snap, execute DPB, wait OIDs disappear/appear, 9s sleep, witness rates sane, bytes monotone | Task 2 Phase 2 |
| Phase 3: physics bound `delta ≤ speed × elapsed × 1.1`, monotone, 12s sleep, growing | Task 2 Phase 3 |
| Phase 4: pre_restore_snap, DPB restore, wait OIDs, 9s sleep, ALL 12 rates sane, bytes monotone | Task 2 Phase 4 |
| Finally: restore 4x25G, wait OIDs, re-add VLAN 10 | Task 2 finally block |
| `_get_speed_bps` — CONFIG_DB read, 25G fallback | Task 1 helpers |
| `_assert_rates_sane` — reuses `_get_oid`, RATES:oid RX/TX_BPS, 110% ceiling | Task 1 helpers |
| `_assert_bytes_monotone` — pure dict, IN+OUT both checked | Task 1 helpers |
| `_assert_bytes_bounded` — delta ≤ speed × elapsed × 1.1, delta ≥ 0 | Task 1 helpers |
| `_assert_bytes_growing` — two snapshots wait_s apart, at least one port grew, skip if no link-up | Task 1 helpers |
| Failure messages: rate explosion, backwards, physics violation | All helpers |
| No iperf3 dependency | ✓ |
| Finally always restores Ethernet80 + VLAN 10 | ✓ |
| Independent from test_breakout_transition (different subject port: Ethernet80 vs Ethernet0) | ✓ |

**Placeholder scan:** None found — all steps contain complete code.

**Type consistency:**
- `_counters_snapshot(ssh, ports)` → `{port: (in, out)}` — used consistently in `_assert_bytes_monotone` and `_assert_bytes_bounded` via index `[0]`/`[1]`.
- `_get_oid(ssh, port)` → `str` — used in `_assert_rates_sane` consistently with all other callers.
- `speed_bps_by_port` dict built from `_get_speed_bps` (returns `float`) — passed to `_assert_bytes_bounded` which reads `.get(port, 3_125_000)`, matching fallback in `_get_speed_bps`.
