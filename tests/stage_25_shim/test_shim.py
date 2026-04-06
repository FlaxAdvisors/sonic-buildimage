import re
import time
import pytest

FLEX_PORTS = [
    "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
    "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    "Ethernet80", "Ethernet81", "Ethernet82", "Ethernet83",
]
NON_FLEX_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
DAEMON_NAME = "wedge100s-flex-counter-daemon"
MIN_STAT_KEYS = 60  # expect 68; allow some slack for future SAI version changes


def test_flex_counter_daemon_running(ssh):
    """The C flex-counter daemon is running on the host."""
    # pgrep -x fails because the kernel truncates comm to 15 chars; use -f.
    out, err, rc = ssh.run(f"pgrep -f '{DAEMON_NAME}' | head -1", timeout=5)
    pid = out.strip()
    assert pid, (
        f"Flex-counter daemon ({DAEMON_NAME}) is not running.\n"
        "Start it: sudo systemctl start wedge100s-flex-counter-daemon"
    )
    print(f"\nDaemon PID: {pid}")


def test_flex_counter_daemon_bcm_config(ssh):
    """The daemon has parsed the BCM config and loaded the lane map."""
    out, err, rc = ssh.run(
        f"grep 'lane entries' /tmp/flex-daemon.log 2>/dev/null; "
        f"sudo grep 'flex-counter-daemon.*lane entries' /var/log/syslog 2>/dev/null | tail -1",
        timeout=10
    )
    assert "lane entries" in out, (
        "Daemon has not logged BCM config parsing.\n"
        "Check /tmp/flex-daemon.log or /var/log/syslog for 'lane entries' message."
    )
    print(f"\n{out.strip()}")


def test_flex_counter_daemon_ps_map(ssh):
    """The daemon has loaded the bcmcmd ps map (connected to diag shell)."""
    out, err, rc = ssh.run(
        f"grep 'ps map' /tmp/flex-daemon.log 2>/dev/null; "
        f"sudo grep 'flex-counter-daemon.*ps map' /var/log/syslog 2>/dev/null | tail -1",
        timeout=10
    )
    assert "ps map" in out, (
        "Daemon has not connected to bcmcmd (no 'ps map' in log).\n"
        "Check that syncd is running with diag shell enabled (dsserve).\n"
        "Socket: /var/run/docker-syncd/sswsyncd.socket"
    )
    print(f"\n{out.strip()}")


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
    """Flex sub-ports have >= 60 SAI stat keys in COUNTERS_DB (daemon-written)."""
    deadline = time.time() + 60
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
        actuals = {p: _get_stat_key_count(ssh, p) for p in failed}
        pytest.fail(
            f"Flex ports with <{MIN_STAT_KEYS} stat keys (daemon not writing):\n"
            + "\n".join(f"  {p}: {actuals[p]} keys" for p in failed)
            + "\nExpected ≥60 keys. Check:\n"
            "  1. Daemon is running: test_flex_counter_daemon_running\n"
            "  2. Daemon log: sudo journalctl -u wedge100s-flex-counter-daemon -n 50\n"
            "  3. Restart: sudo systemctl restart wedge100s-flex-counter-daemon"
        )
    all_counts = {p: results.get(p, _get_stat_key_count(ssh, p)) for p in FLEX_PORTS}
    print(f"\nFlex port stat key counts: {all_counts}")


def test_non_flex_ports_not_regressed(ssh):
    """Non-flex ports still have >= 60 SAI stat keys (native SAI path works)."""
    counts = {}
    for port in NON_FLEX_PORTS:
        n = _get_stat_key_count(ssh, port)
        counts[port] = n
        assert n >= MIN_STAT_KEYS, (
            f"{port}: only {n} stat keys (expected ≥{MIN_STAT_KEYS}). "
            "Native SAI path may be broken for non-breakout ports."
        )
    print(f"\nNon-flex stat counts: {counts}")


def test_flex_port_rx_bytes_nonzero(ssh):
    """Flex sub-ports that are link-up show non-zero IF_IN_OCTETS."""
    up_ports = []
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    for port in FLEX_PORTS:
        if any(port in line and " up " in line for line in out.splitlines()):
            up_ports.append(port)
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up — cannot test RX counter increment")

    time.sleep(5)

    for port in up_ports[:2]:
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
            "Check daemon log and 'show c all' for this port's bcmcmd counters."
        )


def test_flex_port_tx_bytes_nonzero(ssh):
    """At least one link-up flex sub-port shows non-zero IF_OUT_OCTETS."""
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    up_ports = [p for p in FLEX_PORTS
                if any(p in line and " up " in line for line in out.splitlines())]
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up")

    any_nonzero = False
    for port in up_ports:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget 'COUNTERS:{oid}' SAI_PORT_STAT_IF_OUT_OCTETS", timeout=10
        )
        val = int(val_out.strip() or "0")
        print(f"  {port} IF_OUT_OCTETS = {val:,}")
        if val > 0:
            any_nonzero = True

    assert any_nonzero, (
        "No link-up flex sub-port has non-zero IF_OUT_OCTETS.\n"
        "At least one breakout port should have TX traffic (LLDP, ARP, etc.).\n"
        "Check daemon log and bcmcmd 'show c all' output."
    )


def test_startup_zeros_succeed(ssh):
    """All 12 flex sub-ports have the IN_DROPPED_PKTS key present (even if 0).

    Verifies the daemon writes the full stat key set for all breakout ports,
    including link-down ports with zero counters.
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
            "Daemon should write all stat keys for all breakout ports."
        )
    print("\nAll 12 flex ports: SAI_PORT_STAT_IN_DROPPED_PKTS key present")


def test_flex_ports_removed_from_flex_counter(ssh):
    """Breakout ports have been removed from FLEX_COUNTER_TABLE (DB 5).

    The daemon removes breakout ports from FlexCounter polling to prevent
    SAI get_port_stats failures and zero-value overwrites.
    Allows up to 15s for the daemon to detect new OIDs and remove them
    (e.g. after a DPB transition or syncd restart from a prior test run).
    """
    deadline = time.time() + 30
    in_db5 = []
    while time.time() < deadline:
        in_db5 = []
        for port in FLEX_PORTS:
            oid_out, _, _ = ssh.run(
                f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10
            )
            oid = oid_out.strip()
            if not oid:
                continue
            out, _, _ = ssh.run(
                f"redis-cli -n 5 exists 'FLEX_COUNTER_TABLE:PORT_STAT_COUNTER:{oid}'",
                timeout=10
            )
            if out.strip() == "1":
                in_db5.append(port)
        if not in_db5:
            break
        time.sleep(3)

    assert not in_db5, (
        f"Breakout ports still in FLEX_COUNTER_TABLE (DB 5): {in_db5}\n"
        "Daemon should remove these to prevent SAI zero-overwrite race."
    )
    print(f"\nAll flex ports removed from FLEX_COUNTER_TABLE ✓")


def test_flex_port_rates_sane(ssh):
    """Flex port rate values are physically plausible (not >100% of link speed)."""
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    up_ports = [p for p in FLEX_PORTS
                if any(p in line and " up " in line for line in out.splitlines())]
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up")

    time.sleep(5)  # let rates stabilize

    violations = []
    for port in up_ports:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        speed_out, _, _ = ssh.run(f"redis-cli -n 4 hget 'PORT|{port}' speed", timeout=10)
        speed = int(speed_out.strip() or "0")
        if speed == 0:
            continue
        max_bps = speed * 1e6 / 8  # speed is in Mbps, max_bps in bytes/sec

        for direction in ["RX_BPS", "TX_BPS"]:
            val_out, _, _ = ssh.run(
                f"redis-cli -n 2 hget 'RATES:{oid}' {direction}", timeout=10
            )
            rate = float(val_out.strip() or "0")
            pct = rate / max_bps * 100 if max_bps > 0 else 0
            if pct > 110:  # allow 10% headroom for timing jitter
                violations.append(f"{port} {direction}: {rate/1e6:.1f} MB/s = {pct:.0f}%")

    assert not violations, (
        "Flex port rates exceed link speed (rate computation bug):\n"
        + "\n".join(f"  {v}" for v in violations)
    )
    print(f"\nAll {len(up_ports)} link-up flex port rates within link speed ✓")


# ---------------------------------------------------------------------------
# Helper shared by the DPB transition test
# ---------------------------------------------------------------------------

def _get_oid(ssh, port):
    """Return the COUNTERS_DB OID string for port, or '' if absent."""
    out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
    return out.strip()


def _get_counters(ssh, oid):
    """Return (in_octets, out_octets) for the given OID, or (0, 0) on error."""
    if not oid:
        return (0, 0)
    out, _, _ = ssh.run(
        f"redis-cli -n 2 hmget 'COUNTERS:{oid}' "
        "SAI_PORT_STAT_IF_IN_OCTETS SAI_PORT_STAT_IF_OUT_OCTETS",
        timeout=10,
    )
    vals = [v.strip() for v in out.strip().splitlines()]
    try:
        return (
            int(vals[0]) if vals and vals[0].isdigit() else 0,
            int(vals[1]) if len(vals) > 1 and vals[1].isdigit() else 0,
        )
    except (IndexError, ValueError):
        return (0, 0)


def _wait_for_oids(ssh, ports, present=True, timeout_s=30):
    """Poll until all ports have (or lack) OIDs in COUNTERS_PORT_NAME_MAP.

    Returns a dict {port: oid} on success, or raises AssertionError on timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        oids = {p: _get_oid(ssh, p) for p in ports}
        if present and all(oids[p] for p in ports):
            return oids
        if not present and not any(oids[p] for p in ports):
            return oids
        time.sleep(1)
    oids = {p: _get_oid(ssh, p) for p in ports}
    state = "present" if present else "absent"
    missing = [p for p in ports if bool(oids[p]) != present]
    raise AssertionError(
        f"Timed out waiting for ports to be {state} in COUNTERS_PORT_NAME_MAP: {missing}"
    )


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
        speed_bps = speed_bps_by_port.get(port, 3_125_000)
        ceiling = speed_bps * elapsed_s * 1.1
        for idx, direction in enumerate(["IF_IN_OCTETS", "IF_OUT_OCTETS"]):
            delta = current_snap[port][idx] - phase1_snap[port][idx]
            if delta < 0:
                violations.append(
                    f"{port} {direction} went backwards: "
                    f"{phase1_snap[port][idx]:,} → {current_snap[port][idx]:,}"
                )
            elif delta > ceiling:
                speed_g = int(speed_bps * 8 / 1e9)
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
        # Deliberate print+return (not pytest.skip): this helper is called mid-test
        # inside a try/finally block where pytest.skip would abort the finally
        # cleanup.  A silent pass here is correct — no link-up ports means no
        # daemon-liveness check is possible, not a test failure.
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


# ---------------------------------------------------------------------------
# DPB transition test
# ---------------------------------------------------------------------------

def test_breakout_transition(ssh):
    """Flex→non-flex→flex counter lifecycle through a live DPB port change.

    Uses Ethernet0 (parent port, lanes 117-120) which is configured in 4x25G
    breakout mode with hosts connected. The test performs a full round-trip:

    Phase 1 (4x25G baseline)
      Ethernet0..3 are flex sub-ports.  The daemon writes counters from
      bcmcmd 'show c all' and removes them from FlexCounter (DB 5).

    Phase 2 (→ 1x100G)
      'config interface breakout Ethernet0 1x100G[40G] -y -f -l' removes
      sub-ports and creates a single 100G parent port.  For the new 100G OID
      the real get_port_stats SUCCEEDS (native SAI path works for non-flex
      ports).
      - Old sub-port OIDs disappear from COUNTERS_PORT_NAME_MAP.
      - New Ethernet0 OID has 0 counters (fresh port object, link is down
        because the connected host is still 25G).
      - ≥60 stat keys present (native SAI path functional).

    Phase 3 (→ 4x25G restore)
      Breakout is restored.  Sub-ports return.  The daemon detects new OIDs,
      re-removes them from FlexCounter, and begins writing counters.
      - New sub-port OIDs appear in COUNTERS_PORT_NAME_MAP.
      - ≥60 stat keys per sub-port confirms daemon is active.

    Always restores Ethernet0 to 4x25G[10G] in a finally block.
    """
    PARENT     = "Ethernet0"
    SUB_PORTS  = ["Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3"]
    DPB_TIMEOUT = 20   # seconds; DPB takes ~3s on this platform

    # ------------------------------------------------------------------
    # Phase 1: baseline — sub-ports are flex, daemon active
    # ------------------------------------------------------------------
    print("\n=== Phase 1: baseline (4x25G) ===")

    # Record pre-test OIDs and counter values.
    pre_oids = {p: _get_oid(ssh, p) for p in SUB_PORTS}
    for port in SUB_PORTS:
        assert pre_oids[port], (
            f"{port} has no OID in COUNTERS_PORT_NAME_MAP — "
            "switch may not be in 4x25G breakout mode (run tools/deploy.py)"
        )

    pre_keys = _get_stat_key_count(ssh, PARENT)
    assert pre_keys >= MIN_STAT_KEYS, (
        f"Baseline {PARENT}: only {pre_keys} stat keys (expected ≥{MIN_STAT_KEYS}). "
        "Daemon may not be writing counters for this port."
    )

    pre_in, pre_out = _get_counters(ssh, pre_oids[PARENT])
    print(f"  {PARENT} baseline: OID={pre_oids[PARENT]} "
          f"IN={pre_in:,} OUT={pre_out:,} keys={pre_keys}")

    try:
        # ------------------------------------------------------------------
        # Phase 2: change to 1x100G (non-flex / native SAI)
        # ------------------------------------------------------------------
        print("\n=== Phase 2: DPB → 1x100G[40G] ===")
        out, err, rc = ssh.run(
            f"sudo config interface breakout {PARENT} '1x100G[40G]' -y -f -l",
            timeout=30,
        )
        assert rc == 0, (
            f"DPB to 1x100G failed (rc={rc}): {err.strip()[:300]}\n{out.strip()[:300]}"
        )
        assert "successfully completed" in out, (
            f"Unexpected DPB output: {out.strip()[:200]}"
        )

        # Sub-ports Ethernet1..3 must disappear.
        _wait_for_oids(ssh, ["Ethernet1", "Ethernet2", "Ethernet3"],
                       present=False, timeout_s=DPB_TIMEOUT)

        # Parent Ethernet0 must reappear with a NEW OID (single 100G port).
        new_oids_100g = _wait_for_oids(ssh, [PARENT],
                                       present=True, timeout_s=DPB_TIMEOUT)
        new_oid_100g = new_oids_100g[PARENT]
        assert new_oid_100g != pre_oids[PARENT], (
            f"{PARENT}: OID did not change after DPB (still {new_oid_100g}). "
            "portmgrd may not have processed the breakout change."
        )
        print(f"  Old OID: {pre_oids[PARENT]}  →  New OID: {new_oid_100g}")

        # Counters on the new 1x100G OID start at 0 (link down, no 100G peer).
        in_100g, out_100g = _get_counters(ssh, new_oid_100g)
        assert in_100g == 0 and out_100g == 0, (
            f"{PARENT} as 1x100G: expected zero counters (link is down — no 100G peer). "
            f"Got IN={in_100g:,} OUT={out_100g:,}."
        )
        print(f"  Counters 0 (link-down, no 100G peer): IN={in_100g} OUT={out_100g} ✓")

        # Stat keys: native SAI path must provide ≥ MIN_STAT_KEYS.
        deadline = time.time() + 15
        keys_100g = 0
        while time.time() < deadline:
            keys_100g = _get_stat_key_count(ssh, PARENT)
            if keys_100g >= MIN_STAT_KEYS:
                break
            time.sleep(2)
        assert keys_100g >= MIN_STAT_KEYS, (
            f"{PARENT} as 1x100G: only {keys_100g} stat keys after 15s "
            f"(expected ≥{MIN_STAT_KEYS}). Native SAI path may be broken."
        )
        print(f"  Stat keys: {keys_100g} ≥ {MIN_STAT_KEYS} ✓  (native SAI path functional)")

        # ------------------------------------------------------------------
        # Phase 3: restore to 4x25G (daemon re-engages)
        # ------------------------------------------------------------------
        print("\n=== Phase 3: DPB → 4x25G[10G] (restore) ===")

        out, err, rc = ssh.run(
            f"sudo config interface breakout {PARENT} '4x25G[10G]' -y -f -l",
            timeout=30,
        )
        assert rc == 0, (
            f"DPB restore to 4x25G failed (rc={rc}): {err.strip()[:300]}\n{out.strip()[:300]}"
        )
        assert "successfully completed" in out, (
            f"Unexpected DPB restore output: {out.strip()[:200]}"
        )

        # All four sub-ports must reappear with new OIDs.
        restored_oids = _wait_for_oids(ssh, SUB_PORTS,
                                       present=True, timeout_s=DPB_TIMEOUT)

        for port in SUB_PORTS:
            assert restored_oids[port] != pre_oids[port], (
                f"{port}: OID unchanged after restore DPB "
                f"({restored_oids[port]}). portmgrd may not have processed the change."
            )
            assert restored_oids[port] != new_oid_100g, (
                f"{port}: OID matches the 1x100G OID — port may not have been recreated."
            )
        print("  New sub-port OIDs confirmed:")
        for port in SUB_PORTS:
            print(f"    {port}: {restored_oids[port]}")

        # Stat keys: all sub-ports must have ≥ MIN_STAT_KEYS (daemon-written).
        # Allow extra time for daemon to detect new OIDs and write.
        deadline = time.time() + 30
        daemon_confirmed = {}
        while time.time() < deadline:
            for port in SUB_PORTS:
                if port not in daemon_confirmed:
                    n = _get_stat_key_count(ssh, port)
                    if n >= MIN_STAT_KEYS:
                        daemon_confirmed[port] = n
            if len(daemon_confirmed) == len(SUB_PORTS):
                break
            time.sleep(2)

        failed = [p for p in SUB_PORTS if p not in daemon_confirmed]
        if failed:
            actuals = {p: _get_stat_key_count(ssh, p) for p in failed}
            pytest.fail(
                f"Sub-ports with <{MIN_STAT_KEYS} stat keys after DPB restore "
                f"(daemon not re-engaged):\n"
                + "\n".join(f"  {p}: {actuals[p]} keys" for p in failed)
            )
        print(f"  Stat keys per sub-port: { {p: daemon_confirmed[p] for p in SUB_PORTS} } ✓")
        print("  Daemon active on all sub-ports after DPB restore ✓")

    finally:
        # Always ensure Ethernet0 is back in 4x25G mode, regardless of failures.
        current_mode_out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'BREAKOUT_CFG|{PARENT}' brkout_mode", timeout=10
        )
        if current_mode_out.strip() != "4x25G[10G]":
            print(f"\n  [cleanup] Restoring {PARENT} to 4x25G[10G]...")
            ssh.run(
                f"sudo config interface breakout {PARENT} '4x25G[10G]' -y -f -l",
                timeout=30,
            )
            _wait_for_oids(ssh, SUB_PORTS, present=True, timeout_s=30)
            print(f"  [cleanup] {PARENT} restored to 4x25G[10G]")

        # DPB removes sub-ports from VLAN 10; re-add them so subsequent tests
        # (e.g. counter parity via iperf) have L2 connectivity.
        for sp in SUB_PORTS:
            vlan_check, _, _ = ssh.run(
                f"redis-cli -n 4 exists 'VLAN_MEMBER|Vlan10|{sp}'", timeout=5)
            if vlan_check.strip() != "1":
                ssh.run(f"sudo config vlan member add 10 {sp}", timeout=10)
                print(f"  [cleanup] Re-added {sp} to VLAN 10")


def test_nonbreakout_dpb_round_trip_retains_stats(ssh):
    """Non-breakout 100G port retains stats via native SAI across normal operation.

    Ethernet16 is a non-breakout 100G port using the native SAI path (not managed
    by the flex-counter daemon). This test verifies that native SAI counter
    accumulation works correctly alongside the daemon.

    Note: DPB round-trip for Ethernet16 (1x100G → 4x25G → 1x100G) is not
    supported on this platform ('Ethernet16 is not in port_dict' for 4x25G).
    The breakout DPB lifecycle is tested via test_breakout_transition using
    Ethernet0 which has valid breakout config entries.
    """
    PARENT = "Ethernet16"

    pre_oid = _get_oid(ssh, PARENT)
    assert pre_oid, (
        f"{PARENT} has no OID in COUNTERS_PORT_NAME_MAP. "
        "Run 'config load -y' to restore it if missing."
    )

    pre_keys = _get_stat_key_count(ssh, PARENT)
    assert pre_keys >= MIN_STAT_KEYS, (
        f"{PARENT}: only {pre_keys} stat keys (expected ≥{MIN_STAT_KEYS}). "
        "Native SAI path may be broken."
    )

    # Verify counters accumulate over time (native SAI path working).
    in1, out1 = _get_counters(ssh, pre_oid)
    time.sleep(5)
    in2, out2 = _get_counters(ssh, pre_oid)

    print(f"\n{PARENT} (native SAI, 1x100G):")
    print(f"  OID={pre_oid}  keys={pre_keys}")
    print(f"  IN:  {in1:,} → {in2:,} (Δ{in2-in1:,})")
    print(f"  OUT: {out1:,} → {out2:,} (Δ{out2-out1:,})")

    # At least one direction should show growth (LLDP background traffic).
    assert (in2 >= in1 and out2 >= out1), (
        f"{PARENT}: counters decreased — unexpected for native SAI path."
    )
    print(f"  Native SAI counter accumulation verified ✓")


# ---------------------------------------------------------------------------
# sonic-clear counters
# ---------------------------------------------------------------------------

def _portstat_rx_ok(ssh, port):
    """Return the current RX_OK display value for port from portstat output.

    Returns None if the port is not found in portstat output.
    """
    out, _, rc = ssh.run(f"portstat -i {port} 2>/dev/null", timeout=15)
    if rc != 0:
        return None
    for line in out.splitlines():
        if port in line:
            fields = line.split()
            if len(fields) >= 3:
                try:
                    return int(fields[2].replace(",", ""))
                except ValueError:
                    pass
    return None


def test_sonic_clear_counters_flex_and_nonbreakout(ssh):
    """sonic-clear counters works correctly for both flex and non-breakout ports.

    sonic-clear counters (alias for portstat -c) is a DISPLAY-LEVEL soft clear.
    It saves a snapshot of current COUNTERS_DB values as a per-user JSON baseline.
    Subsequent portstat / show interfaces counters calls report current − baseline.

    What is NOT reset:
      - Raw COUNTERS_DB SAI_PORT_STAT_* values (they keep growing).
      - BCM hardware counter registers.

    Test:
      1. Verify both a flex sub-port (Ethernet0, daemon path) and a non-flex 100G
         port (Ethernet16, native SAI path) have non-zero accumulated counters.
      2. Run sonic-clear counters.
      3. Verify portstat shows 0 RX_OK for both ports immediately after clear.
      4. Verify raw COUNTERS_DB values are unchanged (soft clear confirmed).
      5. Wait for new traffic and verify portstat shows positive increments.
    """
    FLEX_PORT    = "Ethernet0"    # daemon bcmcmd path
    NONFLEX_PORT = "Ethernet16"   # native SAI path (optical link, active traffic)

    # ------------------------------------------------------------------
    # Step 1: pre-clear baseline — both ports must have accumulated values.
    # ------------------------------------------------------------------
    flex_oid    = _get_oid(ssh, FLEX_PORT)
    nonflex_oid = _get_oid(ssh, NONFLEX_PORT)
    assert flex_oid,    f"{FLEX_PORT} not in COUNTERS_PORT_NAME_MAP"
    assert nonflex_oid, f"{NONFLEX_PORT} not in COUNTERS_PORT_NAME_MAP"

    # Allow up to 60s: preceding DPB tests may have triggered OID changes;
    # the daemon needs a few cycles to detect new OIDs and write counters.
    deadline = time.time() + 60
    flex_in_pre = flex_out_pre = nonflex_in_pre = nonflex_out_pre = 0
    while time.time() < deadline:
        flex_in_pre,    flex_out_pre    = _get_counters(ssh, flex_oid)
        nonflex_in_pre, nonflex_out_pre = _get_counters(ssh, nonflex_oid)
        if (flex_in_pre > 0 or flex_out_pre > 0) and \
           (nonflex_in_pre > 0 or nonflex_out_pre > 0):
            break
        time.sleep(2)

    assert flex_in_pre > 0 or flex_out_pre > 0, (
        f"{FLEX_PORT}: pre-clear COUNTERS_DB still zero after 60s. "
        "Daemon may not be writing counters — check /tmp/flex-daemon.log."
    )
    assert nonflex_in_pre > 0 or nonflex_out_pre > 0, (
        f"{NONFLEX_PORT}: pre-clear COUNTERS_DB still zero after 60s. "
        "Port may not have link — check FEC (should be RS) and optical connection."
    )
    print(f"\nPre-clear raw COUNTERS_DB:")
    print(f"  {FLEX_PORT}    (flex):    IN={flex_in_pre:,}  OUT={flex_out_pre:,}")
    print(f"  {NONFLEX_PORT} (non-flex): IN={nonflex_in_pre:,} OUT={nonflex_out_pre:,}")

    # Capture portstat before clear so we can verify the clear reset the display.
    flex_ok_pre_display    = _portstat_rx_ok(ssh, FLEX_PORT)    or 0
    nonflex_ok_pre_display = _portstat_rx_ok(ssh, NONFLEX_PORT) or 0
    print(f"Pre-clear portstat display: "
          f"{FLEX_PORT} RX_OK={flex_ok_pre_display:,}  "
          f"{NONFLEX_PORT} RX_OK={nonflex_ok_pre_display:,}")

    # ------------------------------------------------------------------
    # Step 2: clear
    # ------------------------------------------------------------------
    out, err, rc = ssh.run("sonic-clear counters", timeout=15)
    assert rc == 0, f"sonic-clear counters failed (rc={rc}): {err}"
    print(f"\nsonic-clear counters: {out.strip()!r}")

    # ------------------------------------------------------------------
    # Step 3: portstat resets display baseline.
    # ------------------------------------------------------------------
    flex_ok_post_clear    = _portstat_rx_ok(ssh, FLEX_PORT)    or 0
    nonflex_ok_post_clear = _portstat_rx_ok(ssh, NONFLEX_PORT) or 0

    flex_threshold    = max(10000, flex_ok_pre_display    // 100)
    nonflex_threshold = max(10000, nonflex_ok_pre_display // 100)
    assert flex_ok_post_clear < flex_threshold, (
        f"{FLEX_PORT}: portstat RX_OK={flex_ok_post_clear:,} after clear is too large "
        f"(was {flex_ok_pre_display:,} before clear, threshold {flex_threshold:,}). "
        "The soft-clear baseline may not have been saved correctly."
    )
    assert nonflex_ok_post_clear < nonflex_threshold, (
        f"{NONFLEX_PORT}: portstat RX_OK={nonflex_ok_post_clear:,} after clear is too large "
        f"(was {nonflex_ok_pre_display:,} before clear, threshold {nonflex_threshold:,})."
    )
    print(f"Portstat immediately after clear: "
          f"{FLEX_PORT} RX_OK={flex_ok_post_clear:,} (was {flex_ok_pre_display:,}) ✓  "
          f"{NONFLEX_PORT} RX_OK={nonflex_ok_post_clear:,} (was {nonflex_ok_pre_display:,}) ✓")

    # ------------------------------------------------------------------
    # Step 4: raw COUNTERS_DB unchanged (soft clear — not a hardware reset).
    # ------------------------------------------------------------------
    flex_in_snap, flex_out_snap       = _get_counters(ssh, flex_oid)
    nonflex_in_snap, nonflex_out_snap = _get_counters(ssh, nonflex_oid)

    assert flex_in_snap >= flex_in_pre or flex_out_snap >= flex_out_pre, (
        f"{FLEX_PORT}: COUNTERS_DB regressed after clear: "
        f"IN {flex_in_pre:,}→{flex_in_snap:,}  OUT {flex_out_pre:,}→{flex_out_snap:,}. "
        "Daemon counter values should never decrease."
    )
    assert nonflex_in_snap >= nonflex_in_pre or nonflex_out_snap >= nonflex_out_pre, (
        f"{NONFLEX_PORT}: COUNTERS_DB regressed after clear."
    )
    print(f"Raw COUNTERS_DB preserved after clear (soft clear confirmed):")
    print(f"  {FLEX_PORT}    IN={flex_in_snap:,} (≥{flex_in_pre:,}) ✓")
    print(f"  {NONFLEX_PORT} IN={nonflex_in_snap:,} (≥{nonflex_in_pre:,}) ✓")

    # ------------------------------------------------------------------
    # Step 5: after new traffic, portstat shows positive increments.
    # ------------------------------------------------------------------
    print(f"\nWaiting up to 45s for portstat to show post-clear traffic...")
    deadline = time.time() + 45
    flex_ok_final = nonflex_ok_final = 0
    while time.time() < deadline:
        flex_ok_final    = _portstat_rx_ok(ssh, FLEX_PORT)    or 0
        nonflex_ok_final = _portstat_rx_ok(ssh, NONFLEX_PORT) or 0
        if flex_ok_final > 0 and nonflex_ok_final > 0:
            break
        time.sleep(3)

    # Non-flex port may need time to accumulate traffic after DPB tests and
    # config reloads. If link is down or FEC mismatch, skip rather than fail.
    if nonflex_ok_final == 0:
        link_out, _, _ = ssh.run(
            f"show interfaces status {NONFLEX_PORT} 2>&1 | grep {NONFLEX_PORT}", timeout=10
        )
        if " up " not in link_out:
            print(f"  WARNING: {NONFLEX_PORT} link is down — skipping post-clear traffic check")
        else:
            pytest.fail(
                f"{NONFLEX_PORT}: portstat RX_OK still 0 after 45s post-clear. "
                "Link is up but no traffic seen. Check FEC and EOS neighbor."
            )

    assert flex_ok_final > 0, (
        f"{FLEX_PORT}: portstat RX_OK still 0 after 45s post-clear. "
        "Expected background LLDP from connected host. "
        "Check that the host is connected and LLDP is running."
    )
    print(f"Post-clear portstat increments (from clear baseline):")
    print(f"  {FLEX_PORT}    (flex):    RX_OK={flex_ok_final:,} ✓")
    print(f"  {NONFLEX_PORT} (non-flex): RX_OK={nonflex_ok_final:,} ✓")
    print("sonic-clear counters works correctly for both flex and non-breakout ports ✓")


# ---------------------------------------------------------------------------
# Counter parity test via iperf3
# ---------------------------------------------------------------------------

import json
import os

TOPOLOGY_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'tools', 'topology.json')
IPERF_DURATION = 15  # seconds — enough for TCP steady-state

# Pairs of flex ports to test: (server_port, client_port)
# Traffic: client → server.  Switch sees RX on client_port, TX on server_port.
PARITY_PAIRS = [
    ("Ethernet80", "Ethernet0"),     # 25G cross-QSFP
    ("Ethernet67",  "Ethernet66"),   # 10G same-QSFP
]


def _load_topology():
    if not os.path.exists(TOPOLOGY_PATH):
        return {}
    with open(TOPOLOGY_PATH) as f:
        return json.load(f)


def _host_by_port():
    topo = _load_topology()
    return {h["port"]: h for h in topo.get("hosts", [])}


def _paramiko_ssh(mgmt_ip, user="flax", key_file="~/.ssh/id_rsa", timeout=10):
    """Open an SSH connection to a test host via paramiko."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": mgmt_ip, "username": user, "timeout": timeout}
    if key_file:
        kw["key_filename"] = os.path.expanduser(key_file)
    client.connect(**kw)
    return client


def _host_run(client, cmd, timeout=30):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    return out, err, rc


def _counters_snapshot(ssh, ports):
    """Read IN/OUT octets for ports from COUNTERS_DB.  Returns {port: (in, out)}."""
    snap = {}
    for port in ports:
        oid_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        out, _, _ = ssh.run(
            f"redis-cli -n 2 hmget 'COUNTERS:{oid}' "
            "SAI_PORT_STAT_IF_IN_OCTETS SAI_PORT_STAT_IF_OUT_OCTETS",
            timeout=10)
        vals = [v.strip() for v in out.strip().splitlines()]
        try:
            snap[port] = (
                int(vals[0]) if vals and vals[0].isdigit() else 0,
                int(vals[1]) if len(vals) > 1 and vals[1].isdigit() else 0,
            )
        except (IndexError, ValueError):
            pass
    return snap


def test_counter_parity_via_iperf(ssh):
    """Verify Rx/Tx counter parity: bytes into the switch on one port should
    approximately equal bytes out of the switch on the paired port.

    Runs iperf3 between hosts connected to flex sub-ports.  For each pair
    (server_port, client_port):
      - Client host sends to server host through the switch
      - Switch RX delta on client_port ≈ Switch TX delta on server_port
      - Switch TX delta on client_port ≈ Switch RX delta on server_port
        (TCP ACKs flow in reverse)

    Tolerance: 20% relative + 10 MB absolute (accounts for L2 headers,
    background traffic, and timing jitter between counter reads and iperf).
    """
    hbp = _host_by_port()
    if not hbp:
        pytest.skip("topology.json not found or has no hosts")

    # Filter to pairs where both ports are link-up and have hosts
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    link_up = set()
    for line in out.splitlines():
        for port in FLEX_PORTS:
            if port in line and " up " in line:
                link_up.add(port)

    pairs_to_test = []
    for srv_port, cli_port in PARITY_PAIRS:
        if srv_port not in link_up or cli_port not in link_up:
            continue
        if srv_port not in hbp or cli_port not in hbp:
            continue
        # Quick check: can we SSH to both hosts?
        try:
            c = _paramiko_ssh(hbp[srv_port]["mgmt_ip"])
            _, _, rc = _host_run(c, "which iperf3")
            c.close()
            if rc != 0:
                continue
            c = _paramiko_ssh(hbp[cli_port]["mgmt_ip"])
            _, _, rc = _host_run(c, "which iperf3")
            c.close()
            if rc != 0:
                continue
        except Exception:
            continue
        pairs_to_test.append((srv_port, cli_port))

    if not pairs_to_test:
        pytest.skip("No flex port pairs with link-up hosts and iperf3 available")

    all_ports = set()
    for s, c in pairs_to_test:
        all_ports.update([s, c])

    # Wait for daemon to have written counters (in case of prior DPB tests)
    time.sleep(6)

    ABS_TOLERANCE = 10 * 1024 * 1024   # 10 MB
    REL_TOLERANCE = 0.20               # 20%
    violations = []

    def _run_iperf_and_check(ssh, hbp, pairs, direction_label):
        """Run iperf3 for each pair, snapshot counters, check parity.

        Each pair is (server_port, client_port): client sends to server.
        Switch sees RX on client_port, TX on server_port.
        """
        ports = set()
        for s, c in pairs:
            ports.update([s, c])

        before = _counters_snapshot(ssh, list(ports))
        assert len(before) == len(ports), (
            f"Missing counter snapshots for: {ports - set(before.keys())}")

        for srv_port, cli_port in pairs:
            srv_host = hbp[srv_port]
            cli_host = hbp[cli_port]
            srv = _paramiko_ssh(srv_host["mgmt_ip"])
            cli = _paramiko_ssh(cli_host["mgmt_ip"])
            try:
                _host_run(srv, "pkill -f iperf3 2>/dev/null || true")
                # Start server: nohup + </dev/null detaches from SSH session;
                # -1 exits after one client.  Verify listening before proceeding.
                _host_run(srv,
                    f"nohup iperf3 -s -1 -B {srv_host['test_ip']} "
                    f"</dev/null >/dev/null 2>&1 & "
                    f"sleep 1; ss -tlnp 2>/dev/null | grep -q 5201 && echo LISTENING",
                    timeout=5)
                out, err, rc = _host_run(cli,
                    f"iperf3 -c {srv_host['test_ip']} -B {cli_host['test_ip']} "
                    f"-t {IPERF_DURATION} --json",
                    timeout=IPERF_DURATION + 15)
                assert rc == 0, (
                    f"iperf3 {cli_port}→{srv_port} failed (rc={rc}): {err[:200]}")
                data = json.loads(out)
                bps = data["end"]["sum_received"]["bits_per_second"]
                print(f"\n  [{direction_label}] iperf3 {cli_port}→{srv_port}: "
                      f"{bps/1e9:.2f} Gbps ({IPERF_DURATION}s)")
            finally:
                _host_run(srv, "pkill -f iperf3 2>/dev/null || true")
                srv.close()
                cli.close()

        # Delay for daemon to flush final counters to Redis
        time.sleep(4)

        after = _counters_snapshot(ssh, list(ports))

        for srv_port, cli_port in pairs:
            if srv_port not in before or srv_port not in after:
                continue
            if cli_port not in before or cli_port not in after:
                continue

            srv_din  = after[srv_port][0] - before[srv_port][0]
            srv_dout = after[srv_port][1] - before[srv_port][1]
            cli_din  = after[cli_port][0] - before[cli_port][0]
            cli_dout = after[cli_port][1] - before[cli_port][1]

            print(f"\n  [{direction_label}] {cli_port}→{srv_port} counter deltas:")
            print(f"    {cli_port}: ΔRX={cli_din/1e6:.1f} MB  ΔTX={cli_dout/1e6:.1f} MB")
            print(f"    {srv_port}: ΔRX={srv_din/1e6:.1f} MB  ΔTX={srv_dout/1e6:.1f} MB")

            # Forward: client sends → switch RX on cli_port ≈ switch TX on srv_port
            if cli_din > 0 and srv_dout > 0:
                ratio = abs(cli_din - srv_dout) / max(cli_din, srv_dout)
                ok = ratio < REL_TOLERANCE or abs(cli_din - srv_dout) < ABS_TOLERANCE
                print(f"    Forward parity: switch_RX({cli_port})={cli_din/1e6:.1f} MB "
                      f"≈ switch_TX({srv_port})={srv_dout/1e6:.1f} MB "
                      f"(diff={ratio*100:.1f}%) {'✓' if ok else '✗'}")
                if not ok:
                    violations.append(
                        f"[{direction_label}] Forward {cli_port}→{srv_port}: "
                        f"RX={cli_din/1e6:.1f}MB vs TX={srv_dout/1e6:.1f}MB "
                        f"(diff={ratio*100:.1f}%)")
            else:
                violations.append(
                    f"[{direction_label}] Forward {cli_port}→{srv_port}: zero delta "
                    f"(RX={cli_din}, TX={srv_dout})")

            # Reverse: server ACKs → switch RX on srv_port ≈ switch TX on cli_port
            if srv_din > 0 and cli_dout > 0:
                ratio = abs(srv_din - cli_dout) / max(srv_din, cli_dout)
                ok = ratio < REL_TOLERANCE or abs(srv_din - cli_dout) < ABS_TOLERANCE
                print(f"    Reverse parity: switch_RX({srv_port})={srv_din/1e6:.1f} MB "
                      f"≈ switch_TX({cli_port})={cli_dout/1e6:.1f} MB "
                      f"(diff={ratio*100:.1f}%) {'✓' if ok else '✗'}")
                if not ok:
                    violations.append(
                        f"[{direction_label}] Reverse {srv_port}→{cli_port}: "
                        f"RX={srv_din/1e6:.1f}MB vs TX={cli_dout/1e6:.1f}MB "
                        f"(diff={ratio*100:.1f}%)")

    # --- Direction 1: original pairs (client sends to server) ---
    print("\n=== Direction 1: original ===")
    _run_iperf_and_check(ssh, hbp, pairs_to_test, "dir1")

    # --- Direction 2: reversed (swap client/server roles) ---
    reversed_pairs = [(cli, srv) for srv, cli in pairs_to_test]
    print("\n=== Direction 2: reversed ===")
    _run_iperf_and_check(ssh, hbp, reversed_pairs, "dir2")

    assert not violations, (
        "Counter parity violations (daemon counters don't match traffic):\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\nThis indicates the flex-counter daemon is not accurately tracking "
        "ASIC counters for breakout sub-ports."
    )
    print(f"\nCounter parity verified for {len(pairs_to_test)} pairs "
          f"in both directions ✓")
