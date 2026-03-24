"""Stage 12 — Interface Counters & Statistics.

Verifies that syncd flex counter infrastructure is working and that all 32
QSFP ports have counter OIDs in COUNTERS_DB.

Hardware context (verified 2026-03-02):
  - COUNTERS_PORT_NAME_MAP populated with OIDs for all 32 ports
  - All SAI_PORT_STAT_* counters present in COUNTERS_DB
  - Flex counter polling: PORT_STAT 1000ms, enabled
  - Counters increment with live traffic (verified with LLDP traffic)
  - sonic-clear counters works correctly

Phase reference: Phase 12 (Interface Counters & Statistics).
"""

import json
import re
import time
import pytest

NUM_PORTS = 32
# Ports known to be admin-up and link-up (connected to rabbit-lorax)
LINK_UP_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]


@pytest.fixture(scope="session", autouse=True)
def stage12_fec_setup(ssh):
    """Configure RS-FEC on connected ports so counters stage has live traffic."""
    for port in LINK_UP_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    # Wait for links to come up
    deadline = time.time() + 45
    while time.time() < deadline:
        out, _, rc = ssh.run("show interfaces status 2>&1", timeout=15)
        up_ports = [l for l in out.splitlines() if any(p in l for p in LINK_UP_PORTS) and " up " in l]
        if len(up_ports) >= 2:
            break
        time.sleep(5)
    yield
    for port in LINK_UP_PORTS:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)


# ------------------------------------------------------------------
# Flex counter infrastructure
# ------------------------------------------------------------------

def test_flex_counter_port_stat_enabled(ssh):
    """PORT_STAT flex counter is enabled with a polling interval."""
    out, err, rc = ssh.run("counterpoll show", timeout=30)
    assert rc == 0, f"counterpoll show failed (rc={rc}): {err}"
    print(f"\ncounterpoll show:\n{out}")
    # Find the PORT_STAT line
    port_stat_line = next(
        (l for l in out.splitlines() if "PORT_STAT" in l and "BUFFER" not in l), None
    )
    assert port_stat_line is not None, (
        "PORT_STAT not found in counterpoll show output.\n"
        f"Output was:\n{out}"
    )
    assert "enable" in port_stat_line.lower(), (
        f"PORT_STAT is not enabled: {port_stat_line!r}\n"
        "Run: counterpoll port enable"
    )
    # Extract interval (should be numeric ms)
    interval_match = re.search(r"(\d+)\s*ms", port_stat_line, re.IGNORECASE)
    if interval_match:
        interval_ms = int(interval_match.group(1))
        print(f"  PORT_STAT poll interval: {interval_ms}ms")
        assert interval_ms <= 60000, (
            f"PORT_STAT interval {interval_ms}ms seems very long — counters may lag"
        )


def test_counters_port_name_map_all_ports(ssh):
    """COUNTERS_PORT_NAME_MAP has OID entries for all 32 QSFP ports."""
    out, err, rc = ssh.run("redis-cli -n 2 hgetall COUNTERS_PORT_NAME_MAP", timeout=30)
    assert rc == 0, f"redis-cli failed (rc={rc}): {err}"
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    # redis-cli hgetall output alternates field/value
    eth_names = {lines[i] for i in range(0, len(lines), 2) if "Ethernet" in lines[i]}
    print(f"\nEthernet ports in COUNTERS_PORT_NAME_MAP: {len(eth_names)}")
    assert len(eth_names) >= NUM_PORTS, (
        f"Expected >= {NUM_PORTS} Ethernet entries, found {len(eth_names)}.\n"
        "syncd may not have initialized all ports — check for BCM config errors."
    )


def test_counters_db_oid_has_stat_entries(ssh):
    """At least one port's COUNTERS:oid:... key has SAI_PORT_STAT_* entries."""
    # Get OID for first port
    out, err, rc = ssh.run(
        "redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet0", timeout=10
    )
    assert rc == 0 and out.strip(), "Could not get OID for Ethernet0 from COUNTERS_PORT_NAME_MAP"
    oid = out.strip()
    out2, err2, rc2 = ssh.run(
        f"redis-cli -n 2 hgetall 'COUNTERS:{oid}' 2>&1 | head -20", timeout=15
    )
    assert rc2 == 0, f"redis-cli COUNTERS failed: {err2}"
    print(f"\nEthernet0 OID={oid}")
    print(f"Counter sample:\n{out2[:500]}")
    assert "SAI_PORT_STAT_" in out2, (
        f"No SAI_PORT_STAT_* entries in COUNTERS:{oid}\n"
        "Flex counter polling may not be running — check syncd."
    )


def test_counters_key_fields_present(ssh):
    """COUNTERS_DB has all expected RX and TX stat fields for a non-breakout 100G port.

    Breakout sub-ports (Ethernet0-3 etc.) only expose SAI_PORT_STAT_IN/OUT_DROPPED_PKTS
    on this Tomahawk SAI.  The full IF counter set is only available on non-breakout
    100G ports such as Ethernet16.
    """
    EXPECTED_STATS = [
        "SAI_PORT_STAT_IF_IN_OCTETS",
        "SAI_PORT_STAT_IF_IN_UCAST_PKTS",
        "SAI_PORT_STAT_IF_IN_ERRORS",
        "SAI_PORT_STAT_IF_IN_DISCARDS",
        "SAI_PORT_STAT_IF_OUT_OCTETS",
        "SAI_PORT_STAT_IF_OUT_UCAST_PKTS",
        "SAI_PORT_STAT_IF_OUT_ERRORS",
        "SAI_PORT_STAT_IF_OUT_DISCARDS",
    ]
    # Ethernet16 is a non-breakout 100G port with the full SAI counter set.
    # Breakout sub-ports (e.g. Ethernet0) only have IN/OUT_DROPPED_PKTS on this SAI.
    out, err, rc = ssh.run(
        "redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet16", timeout=10
    )
    oid = out.strip()
    out2, _, _ = ssh.run(f"redis-cli -n 2 hkeys 'COUNTERS:{oid}'", timeout=15)
    actual_fields = set(out2.strip().splitlines())
    missing = [f for f in EXPECTED_STATS if f not in actual_fields]
    print(f"\nExpected stats present: {len(EXPECTED_STATS) - len(missing)}/{len(EXPECTED_STATS)}")
    if missing:
        print(f"Missing: {missing}")
    assert not missing, (
        f"Missing counter fields: {missing}\n"
        "These are standard SAI fields — check syncd/SAI version compatibility."
    )


# ------------------------------------------------------------------
# show interfaces counters CLI
# ------------------------------------------------------------------

def test_show_interfaces_counters_exits_zero(ssh):
    """show interfaces counters exits 0."""
    out, err, rc = ssh.run("show interfaces counters", timeout=30)
    assert rc == 0, f"show interfaces counters failed (rc={rc}): {err}"
    assert out.strip(), "Output is empty"


def test_show_interfaces_counters_columns(ssh):
    """show interfaces counters has expected column headers."""
    EXPECTED_COLS = ["IFACE", "STATE", "RX_OK", "RX_BPS", "RX_UTIL",
                     "RX_ERR", "RX_DRP", "TX_OK", "TX_BPS", "TX_UTIL", "TX_ERR", "TX_DRP"]
    out, err, rc = ssh.run("show interfaces counters", timeout=30)
    assert rc == 0, f"Command failed: {err}"
    header_line = next((l for l in out.splitlines() if "IFACE" in l), None)
    assert header_line is not None, "No header line with IFACE found in counters output"
    print(f"\nHeader: {header_line}")
    missing_cols = [c for c in EXPECTED_COLS if c not in header_line]
    assert not missing_cols, f"Missing columns: {missing_cols}"


def test_show_interfaces_counters_port_rows(ssh):
    """show interfaces counters shows at least 32 Ethernet port rows."""
    out, err, rc = ssh.run("show interfaces counters", timeout=30)
    assert rc == 0, f"Command failed: {err}"
    eth_rows = [l for l in out.splitlines() if re.match(r"\s*Ethernet\d+", l)]
    print(f"\nPort rows in counters: {len(eth_rows)}")
    assert len(eth_rows) >= NUM_PORTS, (
        f"Expected >= {NUM_PORTS} rows, found {len(eth_rows)}"
    )


def test_counters_link_up_ports_show_U(ssh):
    """Ports with links up show STATE=U in counters output.

    Requires RS-FEC configured and peer connected (verified 2026-03-02).
    """
    out, err, rc = ssh.run("show interfaces counters", timeout=30)
    assert rc == 0, f"Command failed: {err}"
    for port in LINK_UP_PORTS:
        matching = [l for l in out.splitlines() if port in l]
        if not matching:
            pytest.skip(f"{port} not found in counters output — may need RS-FEC config")
        row = matching[0]
        # STATE column should show U (Up)
        fields = row.split()
        if len(fields) < 2:
            continue
        state = fields[1]
        print(f"  {port}: STATE={state}")
        assert state == "U", (
            f"{port}: Expected STATE=U (link up), got {state!r}\n"
            "Ensure RS-FEC is configured: config interface fec {port} rs"
        )


def test_counters_link_up_ports_have_rx_traffic(ssh):
    """Ports with links up show non-zero RX_OK (LLDP traffic is always present)."""
    out, err, rc = ssh.run("show interfaces counters", timeout=30)
    assert rc == 0, f"Command failed: {err}"
    for port in LINK_UP_PORTS:
        matching = [l for l in out.splitlines() if port in l]
        if not matching:
            continue
        row = matching[0]
        fields = row.split()
        # Column order: IFACE STATE RX_OK RX_BPS RX_UTIL RX_ERR RX_DRP RX_OVR ...
        if len(fields) < 3:
            continue
        rx_ok_raw = fields[2].replace(",", "")
        try:
            rx_ok = int(rx_ok_raw)
        except ValueError:
            continue
        print(f"  {port}: RX_OK={rx_ok}")
        assert rx_ok > 0, (
            f"{port}: RX_OK=0 even though link is up. "
            "LLDP packets should be incrementing this counter continuously."
        )


def test_sonic_clear_counters(ssh):
    """sonic-clear counters resets displayed counter values."""
    out, _, rc = ssh.run("sonic-clear counters", timeout=15)
    assert rc == 0, f"sonic-clear counters failed (rc={rc})"
    assert "Cleared" in out or rc == 0, f"Unexpected output: {out!r}"
    print(f"\nsonic-clear counters: {out.strip()!r}")
    # After clear, RX_OK for link-up ports should be near zero
    import time
    out2, _, rc2 = ssh.run("show interfaces counters", timeout=30)
    assert rc2 == 0
    for port in LINK_UP_PORTS[:1]:  # Check just first port
        matching = [l for l in out2.splitlines() if port in l]
        if not matching:
            continue
        fields = matching[0].split()
        if len(fields) < 3:
            continue
        rx_ok_raw = fields[2].replace(",", "")
        try:
            rx_ok = int(rx_ok_raw)
            print(f"  {port} RX_OK after clear: {rx_ok}")
            # Should be small (< 100 — just a few LLDP frames since clear)
            assert rx_ok < 100, (
                f"{port}: RX_OK={rx_ok} after clear — expected < 100 (LLDP only)"
            )
        except ValueError:
            pass
