"""Stage 13 — Link Status & Basic Connectivity.

Verifies that 100G DAC-connected ports come up and that the full
SONiC port state pipeline (CONFIG_DB → APP_DB → ASIC_DB → STATE_DB)
works correctly.

Hardware topology (from interfaces_connected.md):
  hare-lorax (Wedge 100S, SONiC) ↔ rabbit-lorax (Wedge 100S, Arista EOS)

  Hare Ethernet  | Hare Port | Rabbit Port  | Required FEC
  ---------------|-----------|--------------|-------------
  Ethernet16     | Port 5    | Et13/1       | rs (CL91)
  Ethernet32     | Port 9    | Et14/1       | rs (CL91)
  Ethernet48     | Port 13   | Et15/1       | rs (CL91)
  Ethernet112    | Port 29   | Et16/1       | rs (CL91)

Key finding (verified 2026-03-02):
  100GBASE-CR4 DAC links to Arista require RS-FEC (CL91).
  Default BCM config (phy_an_c73=0x0, no explicit FEC) does NOT enable RS-FEC.
  Fix: config interface fec <port> rs  (persisted to config_db.json via config save)

Phase reference: Phase 13 (Link Status & Basic Connectivity).
"""

import json
import re
import pytest

# Ports connected to rabbit-lorax via 100G DAC (from interfaces_connected.md)
CONNECTED_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]

# Ports known to be admin-up only, not necessarily link-up
ALL_ADMIN_UP = CONNECTED_PORTS + ["Ethernet0"]  # Ethernet0 has breakout cable, no peer


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
    """Read SYS2 LED register (CPLD 0x3f on i2c-1/0x32)."""
    out, _, rc = ssh.run("sudo i2cget -y 1 0x32 0x3f", timeout=10)
    return out.strip() if rc == 0 else None


def test_sys2_led_green_when_link_up(ssh):
    """SYS2 LED (CPLD reg 0x3f on i2c-1/0x32) is green (0x02) when any port is up.

    ledd monitors STATE_DB PORT_TABLE netdev_oper_status and sets SYS2 LED.
    If ledd lost track of port states (e.g. after CPLD reset), the test restarts
    ledd and re-checks before failing.
    """
    val = _read_sys2_led(ssh)
    assert val is not None, "SYS2 LED read failed (i2cget returned non-zero)"
    print(f"\nSYS2 LED (0x3f): {val}")

    if val != "0x02":
        # ledd may have lost track of port states — restart and re-check
        print("  SYS2 LED not green; restarting ledd to resync port states...")
        ssh.run("docker exec pmon supervisorctl restart ledd", timeout=15)
        import time
        time.sleep(3)
        val = _read_sys2_led(ssh)
        print(f"  SYS2 LED after ledd restart: {val}")

    assert val == "0x02", (
        f"SYS2 LED = {val!r}; expected 0x02 (green) after ledd restart.\n"
        "Possible causes:\n"
        "  - ledd not subscribing to STATE_DB PORT_TABLE events\n"
        "  - No ports with netdev_oper_status=up in STATE_DB\n"
        "  - CPLD I2C write failure (check i2c-1 bus)"
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
