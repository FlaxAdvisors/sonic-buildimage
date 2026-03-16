"""Stage 16 — Port Channel / LAG (LACP).

Verifies Link Aggregation Group functionality between hare-lorax (SONiC,
Wedge 100S-32X) and rabbit-lorax (Arista EOS, Wedge 100S).

Prerequisites (configured before tests run):
  - teamd feature enabled on Hare
  - PortChannel1 created on Hare with Ethernet16 + Ethernet32 as members
  - Port-Channel1 created on Rabbit with Et13/1 + Et14/1 in LACP active mode
  - IP addressing: Hare 10.0.1.1/31, Rabbit 10.0.1.0/31

Hardware topology:
  Hare Ethernet  | Hare Port | Rabbit Port  | LAG Role
  ---------------|-----------|--------------|----------
  Ethernet16     | Port 5    | Et13/1       | PortChannel1 member
  Ethernet32     | Port 9    | Et14/1       | PortChannel1 member
  Ethernet48     | Port 13   | Et15/1       | Standalone (control)
  Ethernet112    | Port 29   | Et16/1       | Standalone (control)

Phase reference: Phase 17 (Port Channel / LAG) in INTERFACE_PLAN.md.
Verified on hardware 2026-03-02.
"""

import configparser
import json
import os
import re
import time
import pytest

PORTCHANNEL_NAME = "PortChannel1"
LAG_MEMBERS = ["Ethernet16", "Ethernet32"]
LAG_IP = "10.0.1.1/31"

# Standalone connected ports — should remain unaffected by LAG config
STANDALONE_PORTS = ["Ethernet48", "Ethernet112"]

# Peer IP is read from target.cfg [links] peer_ip; fall back to lab default.
def _load_peer_ip():
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "target.cfg")
    config = configparser.ConfigParser()
    config.read(cfg_path)
    return config.get("links", "peer_ip", fallback="10.0.1.0")

PEER_IP = _load_peer_ip()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _portchannel_summary(ssh):
    """Parse 'show interfaces portchannel' into structured data.

    Returns dict: { "PortChannel1": {"protocol": "LACP(A)(Up)",
                     "members": {"Ethernet16": "S", "Ethernet32": "S"}} }
    """
    out, err, rc = ssh.run("show interfaces portchannel", timeout=30)
    assert rc == 0, f"show interfaces portchannel failed (rc={rc}): {err}"
    result = {}
    for line in out.splitlines():
        m = re.match(
            r"\s*\d+\s+(\S+)\s+(LACP\(\S+\)\(\S+\))\s+(.*)", line
        )
        if m:
            name = m.group(1)
            protocol = m.group(2)
            ports_str = m.group(3).strip()
            members = {}
            for pm in re.finditer(r"(\S+)\(([SsDd\*])\)", ports_str):
                members[pm.group(1)] = pm.group(2)
            result[name] = {"protocol": protocol, "members": members}
    return result


# ------------------------------------------------------------------
# teamd feature state
# ------------------------------------------------------------------

class TestTeamdFeature:
    """Verify teamd container is running."""

    def test_teamd_feature_enabled(self, ssh):
        """teamd feature is enabled in CONFIG_DB."""
        out, err, rc = ssh.run(
            "redis-cli -n 4 hget 'FEATURE|teamd' state", timeout=10
        )
        val = out.strip()
        print(f"  teamd feature state: {val!r}")
        assert val == "enabled", (
            f"teamd feature state={val!r}; expected 'enabled'.\n"
            "Fix: sudo config feature state teamd enabled"
        )

    def test_teamd_container_running(self, ssh):
        """teamd Docker container is running."""
        out, err, rc = ssh.run(
            "docker ps --format '{{.Names}}' --filter name=teamd", timeout=10
        )
        assert "teamd" in out, (
            "teamd container is not running.\n"
            "Fix: sudo config feature state teamd enabled"
        )


# ------------------------------------------------------------------
# PortChannel CONFIG_DB
# ------------------------------------------------------------------

class TestPortChannelConfig:
    """Verify PortChannel1 exists in CONFIG_DB with correct members."""

    def test_portchannel_exists_in_config_db(self, ssh):
        """PORTCHANNEL|PortChannel1 exists in CONFIG_DB."""
        out, _, rc = ssh.run(
            f"redis-cli -n 4 exists 'PORTCHANNEL|{PORTCHANNEL_NAME}'", timeout=10
        )
        assert out.strip() == "1", (
            f"{PORTCHANNEL_NAME} not found in CONFIG_DB.\n"
            f"Fix: sudo config portchannel add {PORTCHANNEL_NAME}"
        )

    def test_portchannel_admin_up(self, ssh):
        """PortChannel1 admin_status is 'up' in CONFIG_DB."""
        out, _, rc = ssh.run(
            f"redis-cli -n 4 hget 'PORTCHANNEL|{PORTCHANNEL_NAME}' admin_status",
            timeout=10,
        )
        val = out.strip()
        print(f"  {PORTCHANNEL_NAME} admin_status: {val!r}")
        assert val == "up"

    def test_portchannel_members_in_config_db(self, ssh):
        """Both member ports are in PORTCHANNEL_MEMBER table."""
        for port in LAG_MEMBERS:
            out, _, rc = ssh.run(
                f"redis-cli -n 4 exists "
                f"'PORTCHANNEL_MEMBER|{PORTCHANNEL_NAME}|{port}'",
                timeout=10,
            )
            assert out.strip() == "1", (
                f"{port} not a member of {PORTCHANNEL_NAME} in CONFIG_DB.\n"
                f"Fix: sudo config portchannel member add {PORTCHANNEL_NAME} {port}"
            )
            print(f"  {PORTCHANNEL_NAME} member {port}: present")

    def test_portchannel_ip_configured(self, ssh):
        """PortChannel1 has IP address configured."""
        out, _, rc = ssh.run(
            f"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|{PORTCHANNEL_NAME}|*'",
            timeout=10,
        )
        assert LAG_IP in out, (
            f"{PORTCHANNEL_NAME} IP {LAG_IP} not in CONFIG_DB.\n"
            f"Fix: sudo config interface ip add {PORTCHANNEL_NAME} {LAG_IP}"
        )
        print(f"  {PORTCHANNEL_NAME} IP: {LAG_IP}")


# ------------------------------------------------------------------
# LACP negotiation state
# ------------------------------------------------------------------

class TestLACPState:
    """Verify LACP is negotiated and both members are selected."""

    def test_portchannel_lacp_active_up(self, ssh):
        """PortChannel1 shows LACP(A)(Up) in portchannel summary."""
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary, (
            f"{PORTCHANNEL_NAME} not in 'show interfaces portchannel' output"
        )
        protocol = summary[PORTCHANNEL_NAME]["protocol"]
        print(f"  {PORTCHANNEL_NAME} protocol: {protocol}")
        assert "Up" in protocol, (
            f"{PORTCHANNEL_NAME} protocol={protocol!r}; expected '(Up)'"
        )

    def test_both_members_selected(self, ssh):
        """Both member ports show (S) = Selected in portchannel summary."""
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary
        members = summary[PORTCHANNEL_NAME]["members"]
        for port in LAG_MEMBERS:
            assert port in members, (
                f"{port} not listed in {PORTCHANNEL_NAME} members: {members}"
            )
            state = members[port]
            print(f"  {port}: state={state!r}")
            assert state == "S", (
                f"{port} state={state!r}; expected 'S' (Selected)"
            )

    def test_teamdctl_state_current(self, ssh):
        """teamdctl reports both ports as 'state: current' (LACP converged)."""
        out, err, rc = ssh.run(
            f"teamdctl {PORTCHANNEL_NAME} state", timeout=15
        )
        assert rc == 0, f"teamdctl state failed (rc={rc}): {err}"
        for port in LAG_MEMBERS:
            assert port in out, f"{port} not in teamdctl output"
        assert "state: current" in out, (
            f"Expected 'state: current' in teamdctl output (LACP converged).\n"
            f"Output:\n{out[:500]}"
        )
        # Verify runner is active
        assert "active: yes" in out, "teamd runner is not active"
        print(f"  teamdctl: runner active, member states current")


# ------------------------------------------------------------------
# APP_DB and STATE_DB propagation
# ------------------------------------------------------------------

class TestDBPropagation:
    """Verify LAG state propagates through the SONiC DB pipeline."""

    def test_lag_table_in_app_db(self, ssh):
        """LAG_TABLE:PortChannel1 exists in APP_DB with oper_status=up."""
        out, _, rc = ssh.run(
            f"redis-cli -n 0 hget 'LAG_TABLE:{PORTCHANNEL_NAME}' oper_status",
            timeout=10,
        )
        val = out.strip()
        print(f"  APP_DB LAG_TABLE oper_status: {val!r}")
        assert val == "up", (
            f"LAG_TABLE:{PORTCHANNEL_NAME} oper_status={val!r} in APP_DB"
        )

    def test_lag_member_table_in_app_db(self, ssh):
        """LAG_MEMBER_TABLE entries exist in APP_DB for both members."""
        for port in LAG_MEMBERS:
            out, _, rc = ssh.run(
                f"redis-cli -n 0 hget "
                f"'LAG_MEMBER_TABLE:{PORTCHANNEL_NAME}:{port}' status",
                timeout=10,
            )
            val = out.strip()
            print(f"  APP_DB LAG_MEMBER {port} status: {val!r}")
            assert val == "enabled", (
                f"LAG_MEMBER_TABLE:{PORTCHANNEL_NAME}:{port} status={val!r}"
            )

    def test_lag_in_state_db(self, ssh):
        """LAG_TABLE|PortChannel1 exists in STATE_DB."""
        out, _, rc = ssh.run(
            f"redis-cli -n 6 hgetall 'LAG_TABLE|{PORTCHANNEL_NAME}'",
            timeout=10,
        )
        assert out.strip(), (
            f"LAG_TABLE|{PORTCHANNEL_NAME} is empty or missing in STATE_DB"
        )
        print(f"  STATE_DB LAG_TABLE: present")


# ------------------------------------------------------------------
# ASIC_DB LAG objects
# ------------------------------------------------------------------

class TestASICDB:
    """Verify SAI LAG and LAG_MEMBER objects in ASIC_DB."""

    def test_sai_lag_object_exists(self, ssh):
        """SAI_OBJECT_TYPE_LAG exists in ASIC_DB for PortChannel1."""
        oid_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget COUNTERS_LAG_NAME_MAP {PORTCHANNEL_NAME}",
            timeout=10,
        )
        oid = oid_out.strip()
        assert oid and oid.startswith("oid:"), (
            f"No OID for {PORTCHANNEL_NAME} in COUNTERS_LAG_NAME_MAP"
        )
        out, _, _ = ssh.run(
            f"redis-cli -n 1 exists 'ASIC_STATE:SAI_OBJECT_TYPE_LAG:{oid}'",
            timeout=10,
        )
        assert out.strip() == "1", (
            f"SAI_OBJECT_TYPE_LAG:{oid} not in ASIC_DB"
        )
        print(f"  ASIC_DB LAG OID: {oid}")

    def test_sai_lag_member_objects_exist(self, ssh):
        """SAI_OBJECT_TYPE_LAG_MEMBER entries exist in ASIC_DB."""
        out, _, _ = ssh.run(
            "redis-cli -n 1 keys 'ASIC_STATE:SAI_OBJECT_TYPE_LAG_MEMBER:*'",
            timeout=10,
        )
        members = [l for l in out.strip().splitlines() if l.strip()]
        assert len(members) >= 2, (
            f"Expected at least 2 LAG_MEMBER objects in ASIC_DB, found {len(members)}"
        )
        print(f"  ASIC_DB LAG_MEMBER count: {len(members)}")


# ------------------------------------------------------------------
# L3 connectivity over LAG
# ------------------------------------------------------------------

class TestLAGConnectivity:
    """Verify IP connectivity over the port channel."""

    def test_portchannel_ip_in_show(self, ssh):
        """PortChannel1 shows IP address in 'show ip interfaces'."""
        out, _, rc = ssh.run("show ip interfaces", timeout=15)
        assert rc == 0
        assert PORTCHANNEL_NAME in out, (
            f"{PORTCHANNEL_NAME} not in 'show ip interfaces' output"
        )
        assert "10.0.1.1" in out, (
            f"IP 10.0.1.1 not shown for {PORTCHANNEL_NAME}"
        )
        print(f"  {PORTCHANNEL_NAME} IP visible in show ip interfaces")

    def test_ping_peer_over_lag(self, ssh):
        """Ping peer (10.0.1.0) over the port channel succeeds."""
        out, err, rc = ssh.run(f"ping -c5 -W2 {PEER_IP}", timeout=20)
        print(f"  Ping output:\n{out}")
        assert rc == 0, f"Ping to {PEER_IP} failed (rc={rc}): {err}"
        # Extract packet loss
        m = re.search(r"(\d+)% packet loss", out)
        assert m, f"Could not parse packet loss from ping output"
        loss = int(m.group(1))
        assert loss == 0, f"Ping to {PEER_IP}: {loss}% packet loss"


# ------------------------------------------------------------------
# LAG failover
# ------------------------------------------------------------------

class TestLAGFailover:
    """Verify LAG survives a single member link failure.

    This test shuts down Ethernet16, verifies the LAG stays up on
    Ethernet32 alone, then restores Ethernet16 and verifies recovery.
    """

    @pytest.fixture(autouse=True)
    def _restore_lag_members(self, ssh):
        """Ensure all LAG member ports are admin-up after each test.

        If the test fails after shutting a member down, this fixture
        brings it back up so the switch is not left in a degraded state.
        """
        yield
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
            )
            if out.strip() != "up":
                ssh.run(f"sudo config interface startup {port}", timeout=15)
                time.sleep(2)

    def test_failover_and_recovery(self, ssh):
        """Shut one member, verify connectivity, restore, verify both selected."""
        fail_port = LAG_MEMBERS[0]  # Ethernet16
        survive_port = LAG_MEMBERS[1]  # Ethernet32

        # --- Phase 1: shut down one member ---
        _, _, rc = ssh.run(
            f"sudo config interface shutdown {fail_port}", timeout=15
        )
        assert rc == 0, f"Failed to shutdown {fail_port}"
        time.sleep(5)  # wait for LACP to converge

        # Verify LAG still up with one member
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary, (
            f"{PORTCHANNEL_NAME} disappeared after shutting {fail_port}"
        )
        protocol = summary[PORTCHANNEL_NAME]["protocol"]
        print(f"  After shutdown {fail_port}: protocol={protocol}")
        assert "Up" in protocol, (
            f"{PORTCHANNEL_NAME} went down after shutting {fail_port}"
        )
        members = summary[PORTCHANNEL_NAME]["members"]
        assert members.get(survive_port) == "S", (
            f"{survive_port} not Selected after {fail_port} shutdown: {members}"
        )
        # Verify ping still works
        out, _, rc = ssh.run(f"ping -c3 -W2 {PEER_IP}", timeout=15)
        m = re.search(r"(\d+)% packet loss", out)
        loss = int(m.group(1)) if m else 100
        print(f"  Ping during failover: {loss}% loss")
        assert loss == 0, f"Ping failed during failover: {loss}% loss"

        # --- Phase 2: restore the member ---
        _, _, rc = ssh.run(
            f"sudo config interface startup {fail_port}", timeout=15
        )
        assert rc == 0, f"Failed to startup {fail_port}"
        time.sleep(8)  # wait for LACP reconvergence

        # Verify both members are back to Selected
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary
        members = summary[PORTCHANNEL_NAME]["members"]
        for port in LAG_MEMBERS:
            state = members.get(port, "MISSING")
            print(f"  After recovery: {port} state={state}")
            assert state == "S", (
                f"{port} state={state!r} after recovery; expected 'S'"
            )

        # Verify ping still works with both members
        out, _, rc = ssh.run(f"ping -c3 -W2 {PEER_IP}", timeout=15)
        m = re.search(r"(\d+)% packet loss", out)
        loss = int(m.group(1)) if m else 100
        assert loss == 0, f"Ping failed after recovery: {loss}% loss"
        print(f"  Recovery complete: both members Selected, ping OK")


# ------------------------------------------------------------------
# Standalone ports unaffected
# ------------------------------------------------------------------

class TestStandalonePortsUnaffected:
    """Verify that non-LAG connected ports remain operational."""

    def test_standalone_ports_still_up(self, ssh):
        """Ethernet48 and Ethernet112 (not in LAG) remain oper=up."""
        out, _, rc = ssh.run("show interfaces status", timeout=30)
        assert rc == 0
        for port in STANDALONE_PORTS:
            m = re.search(
                rf"\s*{port}\s+.*?\s+(up|down)\s+(up|down)", out
            )
            assert m, f"Could not parse status for {port}"
            oper = m.group(1)
            admin = m.group(2)
            print(f"  {port}: oper={oper} admin={admin}")
            assert oper == "up", (
                f"{port} oper={oper!r} — LAG config should not affect standalone ports"
            )
