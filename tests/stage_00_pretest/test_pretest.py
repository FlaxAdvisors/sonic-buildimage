"""Stage 00 — Pre-Test: Operational state audit.

Verifies that tools/deploy.py has been run and the switch is in the
expected operational state before any functional tests execute.

Failure here means "run tools/deploy.py" — these are NOT test failures
in the traditional sense; they indicate missing prerequisite config.
"""

import pytest

CONNECTED_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
BREAKOUT_SUBPORTS = [
    "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
    "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    "Ethernet80", "Ethernet81", "Ethernet82", "Ethernet83",
]


def test_pmon_running(ssh):
    """pmon service is active."""
    out, _, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active: {out.strip()}\nFix: sudo systemctl start pmon"


def test_mgmt_vrf_present(ssh):
    """mgmt VRF is configured."""
    out, _, rc = ssh.run("ip vrf show", timeout=10)
    assert "mgmt" in out, "mgmt VRF missing — run: tools/deploy.py"


def test_breakout_subports_in_asic_db(ssh):
    """All 12 breakout sub-ports are present in COUNTERS_PORT_NAME_MAP (ASIC_DB)."""
    out, _, _ = ssh.run(
        "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
    )
    present = set(out.split())
    missing = [p for p in BREAKOUT_SUBPORTS if p not in present]
    assert not missing, (
        f"Breakout sub-ports missing in ASIC_DB: {missing}\n"
        "Fix: tools/deploy.py --task breakout"
    )


def test_portchannel1_in_config_db(ssh):
    """PortChannel1 exists in CONFIG_DB."""
    out, _, _ = ssh.run(
        r"redis-cli -n 4 EXISTS 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    assert out.strip() == "1", (
        "PortChannel1 missing in CONFIG_DB — run: tools/deploy.py"
    )


def test_portchannel1_has_no_ip(ssh):
    """PortChannel1 has no IP address (L2 VLAN 999 only)."""
    out, _, _ = ssh.run(
        r"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|PortChannel1|*'", timeout=10
    )
    assert not out.strip(), (
        f"PortChannel1 has IP configured (L2 only expected): {out.strip()}\n"
        "Fix: tools/deploy.py --task portchannel"
    )


def test_vlan10_and_999_exist(ssh):
    """VLAN 10 and VLAN 999 are present in CONFIG_DB."""
    for vid in (10, 999):
        out, _, _ = ssh.run(
            f"redis-cli -n 4 EXISTS 'VLAN|Vlan{vid}'", timeout=10
        )
        assert out.strip() == "1", (
            f"VLAN {vid} missing in CONFIG_DB — run: tools/deploy.py"
        )


def test_vlan10_has_breakout_members(ssh):
    """All 12 breakout sub-ports are VLAN 10 members."""
    missing = []
    for port in BREAKOUT_SUBPORTS:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 EXISTS 'VLAN_MEMBER|Vlan10|{port}'", timeout=10
        )
        if out.strip() != "1":
            missing.append(port)
    assert not missing, (
        f"Ports missing from VLAN 10: {missing}\nFix: tools/deploy.py --task vlans"
    )


def test_connected_ports_admin_up(ssh):
    """Connected uplink ports (Ethernet16/32/48/112) are admin-up."""
    for port in CONNECTED_PORTS:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
        )
        assert out.strip() == "up", (
            f"{port} admin_status={out.strip()!r} — expected 'up'\n"
            f"Fix: sudo config interface startup {port}"
        )


def test_optical_ports_fec_configured(ssh):
    """Optical ports (Ethernet100/104/108/116) have FEC=rs in CONFIG_DB."""
    optical = ["Ethernet100", "Ethernet104", "Ethernet108", "Ethernet116"]
    for port in optical:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=10
        )
        assert out.strip() == "rs", (
            f"{port} fec={out.strip()!r} — expected 'rs'\n"
            f"Fix: tools/deploy.py --task optical"
        )
