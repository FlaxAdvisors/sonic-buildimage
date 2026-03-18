"""Stage 13 — Link Status & Basic Connectivity.

Verifies that 100G DAC-connected ports come up and that the full
SONiC port state pipeline (CONFIG_DB → APP_DB → ASIC_DB → STATE_DB)
works correctly.

Hardware topology:
  hare-lorax (Wedge 100S, SONiC) ↔ rabbit-lorax (Wedge 100S, Arista EOS)

Key finding (verified 2026-03-02):
  100GBASE-CR4 DAC links to Arista require RS-FEC (CL91).
  Default BCM config (phy_an_c73=0x0, no explicit FEC) does NOT enable RS-FEC.
  Fix: config interface fec <port> rs  (persisted to config_db.json via config save)

Connected ports and peer IP are read from target.cfg [links] section:

  [links]
  # Space or comma separated list of ports connected to the peer device
  connected_ports = Ethernet16,Ethernet32,Ethernet48,Ethernet112
  # IP address of peer device reachable over the port channel / connected ports
  peer_ip = 10.0.1.0

Defaults (used if [links] section is absent) match the lab topology.

Phase reference: Phase 13 (Link Status & Basic Connectivity).
"""

import configparser
import json
import os
import re
import pytest


def _load_links_config():
    """Load connected_ports and peer_ip from target.cfg [links] section.

    Falls back to lab defaults if the section or keys are absent.
    """
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "target.cfg")
    config = configparser.ConfigParser()
    config.read(cfg_path)

    default_ports = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
    default_peer   = "10.0.1.0"

    if not config.has_section("links"):
        return default_ports, default_peer

    raw_ports = config.get("links", "connected_ports", fallback="")
    ports = [p.strip() for p in raw_ports.replace(",", " ").split() if p.strip()]
    peer  = config.get("links", "peer_ip", fallback=default_peer)
    return (ports or default_ports), peer


# Load once at module level — consistent across all tests in this file
CONNECTED_PORTS, PEER_IP = _load_links_config()


@pytest.fixture(scope="session", autouse=True)
def configure_rsfec(ssh):
    """Configure RS-FEC on connected ports; remove after stage completes."""
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    # Wait for links to come up (up to 30 s)
    import time
    deadline = time.time() + 30
    while time.time() < deadline:
        out, _, rc = ssh.run("show interfaces status 2>&1", timeout=15)
        up_ports = [l for l in out.splitlines() if any(p in l for p in CONNECTED_PORTS) and " up " in l]
        if len(up_ports) >= 2:  # at least 2 of 4 up (Ethernet104/108 blocked)
            break
        time.sleep(3)
    yield
    # Teardown: remove FEC
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _port_status(ssh):
    """Return dict port → {oper, admin, fec, type} from 'show interfaces status'."""
    out, err, rc = ssh.run("show interfaces status", timeout=30)
    assert rc == 0, f"show interfaces status failed (rc={rc}): {err}"
    result = {}
    for line in out.splitlines():
        m = re.match(
            r"\s*(Ethernet\d+)\s+"          # port name
            r"[\d,]+\s+"                    # lanes
            r"(\S+)\s+"                     # speed
            r"\d+\s+"                       # mtu
            r"(\S+)\s+"                     # fec
            r"\S+\s+"                       # alias
            r"(\S+)\s+"                     # type
            r"(\S+)\s+"                     # oper
            r"(\S+)",                       # admin
            line,
        )
        if m:
            result[m.group(1)] = {
                "speed":  m.group(2),
                "fec":    m.group(3),
                "type":   m.group(4),
                "oper":   m.group(5),
                "admin":  m.group(6),
            }
    return result


# ------------------------------------------------------------------
# FEC configuration
# ------------------------------------------------------------------

def test_connected_ports_fec_rs_configured(ssh):
    """Connected ports have RS-FEC configured in CONFIG_DB.

    RS-FEC (CL91) is required for 100GBASE-CR4 links to Arista EOS.
    Without RS-FEC: Arista reports 'FEC alignment lock: unaligned' → link down.
    """
    for port in CONNECTED_PORTS:
        out, err, rc = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=10
        )
        assert rc == 0, f"redis-cli failed for {port}: {err}"
        fec_val = out.strip()
        print(f"  {port}: fec={fec_val!r}")
        assert fec_val == "rs", (
            f"{port}: FEC={fec_val!r} but 'rs' is required for link with Arista.\n"
            f"Fix: sudo config interface fec {port} rs && sudo config save -y"
        )


def test_connected_ports_fec_rs_in_status(ssh):
    """show interfaces status shows fec=rs for connected ports."""
    statuses = _port_status(ssh)
    for port in CONNECTED_PORTS:
        info = statuses.get(port, {})
        fec = info.get("fec", "N/A")
        print(f"  {port}: fec={fec!r}")
        assert fec == "rs", (
            f"{port}: show interfaces status shows fec={fec!r}, expected 'rs'"
        )


# ------------------------------------------------------------------
# Admin status
# ------------------------------------------------------------------

def test_connected_ports_admin_up(ssh):
    """Connected ports are admin-up in CONFIG_DB."""
    for port in CONNECTED_PORTS:
        out, err, rc = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
        )
        admin = out.strip()
        print(f"  {port}: admin_status={admin!r}")
        assert admin == "up", (
            f"{port}: admin_status={admin!r} in CONFIG_DB; expected 'up'.\n"
            f"Fix: sudo config interface startup {port}"
        )


# ------------------------------------------------------------------
# Oper status (link-up)
# ------------------------------------------------------------------

def test_connected_ports_oper_up(ssh):
    """Connected ports show oper=up in show interfaces status.

    Requires:
    - RS-FEC configured (see test_connected_ports_fec_rs_configured)
    - peer device (rabbit-lorax) with admin-up ports
    """
    statuses = _port_status(ssh)
    for port in CONNECTED_PORTS:
        info = statuses.get(port, {})
        oper = info.get("oper", "MISSING")
        admin = info.get("admin", "MISSING")
        print(f"  {port}: admin={admin} oper={oper}")
        if admin != "up":
            pytest.skip(f"{port} is not admin-up — skipping oper status check")
        assert oper == "up", (
            f"{port}: oper_status={oper!r} even though admin=up and RS-FEC is set.\n"
            "Possible causes:\n"
            "  1. Peer device port is down\n"
            "  2. BCM config pre-emphasis mismatch for this cable type\n"
            "  3. syncd/orchagent restart cycle (check 'docker ps')"
        )


# ------------------------------------------------------------------
# APP_DB and ASIC_DB state propagation
# ------------------------------------------------------------------

def test_port_state_in_app_db(ssh):
    """Connected ports appear in APP_DB:PORT_TABLE with correct oper_status."""
    for port in CONNECTED_PORTS:
        out, err, rc = ssh.run(
            f"redis-cli -n 0 hgetall 'PORT_TABLE:{port}'", timeout=10
        )
        assert rc == 0
        assert out.strip(), f"PORT_TABLE:{port} is empty in APP_DB (DB0)"
        print(f"\n  {port} APP_DB: {out.strip()[:200]}")
        assert "oper_status" in out, f"oper_status not in APP_DB PORT_TABLE for {port}"


def test_port_oper_status_state_db(ssh):
    """Connected ports show netdev_oper_status=up in STATE_DB PORT_TABLE."""
    for port in CONNECTED_PORTS:
        out, err, rc = ssh.run(
            f"redis-cli -n 6 hget 'PORT_TABLE|{port}' netdev_oper_status", timeout=10
        )
        val = out.strip()
        print(f"  {port}: STATE_DB netdev_oper_status={val!r}")
        assert val == "up", (
            f"{port}: netdev_oper_status={val!r} in STATE_DB — ledd uses this "
            "to decide SYS2 LED state"
        )


def test_asic_db_port_admin_state(ssh):
    """Connected ports show SAI_PORT_ATTR_ADMIN_STATE=true in ASIC_DB.

    Note: SAI_PORT_ATTR_OPER_STATUS is not stored in ASIC_DB on Memory's SFP code
    for this platform/SAI version — oper_status is verified via STATE_DB
    (test_port_oper_status_state_db) and APP_DB (test_port_state_in_app_db).
    """
    for port in CONNECTED_PORTS:
        oid_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10
        )
        oid = oid_out.strip()
        if not oid:
            pytest.skip(f"No OID for {port} in COUNTERS_PORT_NAME_MAP")
        out, _, _ = ssh.run(
            f"redis-cli -n 1 hget 'ASIC_STATE:SAI_OBJECT_TYPE_PORT:{oid}' "
            f"SAI_PORT_ATTR_ADMIN_STATE",
            timeout=10
        )
        val = out.strip()
        print(f"  {port} ({oid}): SAI_PORT_ATTR_ADMIN_STATE={val!r}")
        assert val == "true", (
            f"{port}: ASIC_DB admin_state={val!r}; expected 'true'"
        )


# ------------------------------------------------------------------
# SYS2 LED
# ------------------------------------------------------------------

def _read_sys2_led(ssh):
    """Read SYS2 LED via wedge100s_cpld sysfs; returns int or None."""
    out, _, rc = ssh.run("cat /sys/bus/i2c/devices/1-0032/led_sys2", timeout=10)
    if rc != 0:
        return None
    try:
        return int(out.strip(), 0)
    except ValueError:
        return None


def test_sys2_led_green_when_link_up(ssh):
    """SYS2 LED is green (0x02) when any port is up.

    ledd monitors STATE_DB PORT_TABLE netdev_oper_status and sets SYS2 LED.
    If ledd lost track of port states (e.g. after CPLD reset), the test restarts
    ledd and re-checks before failing.
    """
    val = _read_sys2_led(ssh)
    assert val is not None, "SYS2 LED read failed (sysfs read returned non-zero or unparseable)"
    print(f"\nSYS2 LED: 0x{val:02x}")

    if val != 0x02:
        # ledd may have lost track of port states — restart and re-check
        print("  SYS2 LED not green; restarting ledd to resync port states...")
        ssh.run("docker exec pmon supervisorctl restart ledd", timeout=15)
        import time
        time.sleep(3)
        val = _read_sys2_led(ssh)
        print(f"  SYS2 LED after ledd restart: {f'0x{val:02x}' if val is not None else 'read failed'}")

    assert val == 0x02, (
        f"SYS2 LED = 0x{val:02x}; expected 0x02 (green) after ledd restart.\n"
        "Possible causes:\n"
        "  - ledd not subscribing to STATE_DB PORT_TABLE events\n"
        "  - No ports with netdev_oper_status=up in STATE_DB\n"
        "  - CPLD sysfs write failure (check wedge100s_cpld driver)"
    )


# ------------------------------------------------------------------
# LLDP neighbor discovery (bonus — Phase 18)
# ------------------------------------------------------------------

def test_lldp_neighbors_on_connected_ports(ssh):
    """LLDP discovers rabbit-lorax as neighbor on all 4 connected ports.

    Tests Layer 2 connectivity end-to-end: SONiC → ASIC → DAC cable → Arista.
    LLDP frames are generated by the lldp container and forwarded by the ASIC.
    """
    out, err, rc = ssh.run("show lldp neighbors", timeout=30)
    assert rc == 0, f"show lldp neighbors failed (rc={rc}): {err}"
    print(f"\nLLDP neighbors:\n{out[:800]}")

    for port in CONNECTED_PORTS:
        assert port in out, (
            f"LLDP neighbor not found on {port}.\n"
            "This could mean:\n"
            "  1. The link is down (check oper_status)\n"
            "  2. lldp container is not running\n"
            "  3. Peer device (rabbit-lorax) LLDP is disabled on this port"
        )
    # Verify rabbit-lorax is the discovered neighbor
    assert "rabbit-lorax" in out, (
        "Neighbor 'rabbit-lorax' not found in LLDP output.\n"
        f"Found output:\n{out[:500]}"
    )


def test_lldp_neighbor_port_mapping(ssh):
    """LLDP neighbor port IDs on connected ports match expected Arista Et13-16 ports."""
    EXPECTED_PEERS = {
        "Ethernet16":  "Ethernet13/1",
        "Ethernet32":  "Ethernet14/1",
        "Ethernet48":  "Ethernet15/1",
        "Ethernet112": "Ethernet16/1",
    }
    out, err, rc = ssh.run("show lldp neighbors", timeout=30)
    assert rc == 0, f"Command failed: {err}"

    # Parse lldp output into sections by interface
    current_iface = None
    iface_data = {}
    for line in out.splitlines():
        m = re.match(r"Interface:\s+(\S+?)(?:,|\s)", line)
        if m:
            current_iface = m.group(1)
            iface_data[current_iface] = line
        elif current_iface:
            iface_data[current_iface] = iface_data.get(current_iface, "") + "\n" + line

    for port, expected_peer_port in EXPECTED_PEERS.items():
        if port not in iface_data:
            pytest.skip(f"No LLDP data for {port} — link may be down")
        assert expected_peer_port in iface_data[port], (
            f"{port}: Expected peer port {expected_peer_port!r} in LLDP data, "
            f"but found:\n{iface_data[port]}"
        )
        print(f"  {port} → {expected_peer_port} ✓")
