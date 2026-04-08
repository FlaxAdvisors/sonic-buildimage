"""Stage 16 — Port Channel / LAG (LACP) — operational state assertions.

Tests assert that PortChannel1 is already configured and operational,
as established by tools/deploy.py. PortChannel1 is L2 access on VLAN 999.
Vlan999 carries a /31 SVI on both sides for ping-based reachability testing:
  SONiC  hare-lorax   Vlan999 10.99.1.1/31
  EOS    rabbit-lorax Vlan999 10.99.1.0/31

Hardware topology:
  Hare Ethernet  | Rabbit Port  | Role
  ---------------|--------------|-----
  Ethernet16     | Et13/1       | PortChannel1 member
  Ethernet32     | Et14/1       | PortChannel1 member
  Ethernet48     | Et15/1       | standalone
  Ethernet112    | Et16/1       | standalone
"""

import re
import time
import pytest

PORTCHANNEL_NAME = "PortChannel1"
LAG_MEMBERS = ["Ethernet16", "Ethernet32"]
STANDALONE_PORTS = ["Ethernet48", "Ethernet112"]

# Vlan999 SVI addresses — /31 point-to-point over the LAG.
SONIC_SVI_IP  = "10.99.1.1"
EOS_SVI_IP    = "10.99.1.0"

TEAMDCTL_POLL_INTERVAL = 2
TEAMDCTL_FAILOVER_TIMEOUT = 10   # seconds for one member to deselect
TEAMDCTL_RECOVER_TIMEOUT = 30    # seconds for both members to reselect
PING_FAILOVER_TIMEOUT = 15       # seconds for ping to recover after member restore


@pytest.fixture(scope="session", autouse=True)
def stage16_ensure_l2(ssh):
    """Remove any L3 IP addresses from PortChannel1 so it operates as L2.

    Prior L3/BGP configuration may have added a routed IP (e.g. 10.0.1.1/31)
    to PortChannel1. With a routed IP, the port becomes L3 and doesn't
    participate in VLAN 999 L2 forwarding — breaking LAG reachability tests.
    """
    out, _, _ = ssh.run(
        f"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|{PORTCHANNEL_NAME}|*'",
        timeout=10,
    )
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Extract IP from key like PORTCHANNEL_INTERFACE|PortChannel1|10.0.1.1/31
        parts = line.split("|")
        if len(parts) >= 3:
            ip = parts[-1]
            print(f"  [setup] Removing L3 IP {ip} from {PORTCHANNEL_NAME}")
            ssh.run(
                f"sudo config interface ip remove {PORTCHANNEL_NAME} {ip}",
                timeout=15,
            )
    # Also remove the bare PORTCHANNEL_INTERFACE entry if it exists (L3 mode marker)
    ssh.run(
        f"redis-cli -n 4 del 'PORTCHANNEL_INTERFACE|{PORTCHANNEL_NAME}'",
        timeout=10,
    )
    yield


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _portchannel_summary(ssh):
    """Parse 'show interfaces portchannel' into structured data."""
    out, err, rc = ssh.run("show interfaces portchannel", timeout=30)
    assert rc == 0, f"show interfaces portchannel failed (rc={rc}): {err}"
    result = {}
    for line in out.splitlines():
        m = re.match(r"\s*\d+\s+(\S+)\s+(LACP\(\S+\)\(\S+\))\s+(.*)", line)
        if m:
            name, protocol, ports_str = m.group(1), m.group(2), m.group(3).strip()
            members = {}
            for pm in re.finditer(r"(\S+)\(([SsDd\*])\)", ports_str):
                members[pm.group(1)] = pm.group(2)
            result[name] = {"protocol": protocol, "members": members}
    return result


def _teamdctl_members(ssh) -> dict:
    """Return {port_name: state_str} from teamdctl PortChannel1 state.

    state_str is 'current' when the member is selected and LACP-converged.

    teamdctl output format (2-space indent for port names under 'ports:'):
      ports:
        Ethernet16
          ...
          runner:
            ...
            state: current
    """
    out, _, rc = ssh.run(f"teamdctl {PORTCHANNEL_NAME} state", timeout=15)
    if rc != 0:
        return {}
    result = {}
    current_port = None
    in_ports_section = False
    in_runner_section = False
    for line in out.splitlines():
        # Detect 'ports:' section header
        if re.match(r"^ports:$", line):
            in_ports_section = True
            continue
        # Detect end of ports section (top-level 'runner:')
        if re.match(r"^runner:$", line):
            in_ports_section = False
            continue
        if not in_ports_section:
            continue
        # Port name: 2-space indent, no leading spaces in name
        m = re.match(r"^  (\S+)$", line)
        if m:
            current_port = m.group(1)
            in_runner_section = False
            continue
        # 'runner:' sub-section under a port (4-space indent)
        if current_port and re.match(r"^    runner:$", line):
            in_runner_section = True
            continue
        # 'state:' key inside runner sub-section (6-space indent)
        if current_port and in_runner_section:
            ms = re.match(r"^      state:\s+(\S+)$", line)
            if ms:
                result[current_port] = ms.group(1)
    return result


def _wait_for_member_state(ssh, port, expected_state, timeout):
    """Poll teamdctl until port reaches expected_state or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        members = _teamdctl_members(ssh)
        if members.get(port) == expected_state:
            return True
        time.sleep(TEAMDCTL_POLL_INTERVAL)
    return False


def _ping_peer(ssh, count=3, timeout_per=2):
    """Ping EOS Vlan999 SVI from SONiC. Returns (success_bool, output_str)."""
    out, _, rc = ssh.run(
        f"ping -c {count} -W {timeout_per} {EOS_SVI_IP}", timeout=count * timeout_per + 10
    )
    return rc == 0, out.strip()


def _wait_for_ping(ssh, timeout, interval=2):
    """Poll ping to EOS peer until it succeeds or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, _ = _ping_peer(ssh, count=1, timeout_per=2)
        if ok:
            return True
        time.sleep(interval)
    return False


# ------------------------------------------------------------------
# teamd feature state
# ------------------------------------------------------------------

class TestTeamdFeature:

    def test_teamd_feature_enabled(self, ssh):
        """teamd feature is enabled in CONFIG_DB."""
        out, _, _ = ssh.run(
            "redis-cli -n 4 hget 'FEATURE|teamd' state", timeout=10
        )
        val = out.strip()
        print(f"  teamd feature state: {val!r}")
        assert val == "enabled", f"teamd feature state={val!r}; expected 'enabled'"

    def test_teamd_container_running(self, ssh):
        """teamd Docker container is running."""
        out, _, _ = ssh.run(
            "docker ps --format '{{.Names}}' --filter name=teamd", timeout=10
        )
        assert "teamd" in out, "teamd container is not running"


# ------------------------------------------------------------------
# PortChannel CONFIG_DB
# ------------------------------------------------------------------

class TestPortChannelConfig:

    def test_portchannel_exists_in_config_db(self, ssh):
        """PORTCHANNEL|PortChannel1 exists in CONFIG_DB."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 exists 'PORTCHANNEL|{PORTCHANNEL_NAME}'", timeout=10
        )
        assert out.strip() == "1", f"{PORTCHANNEL_NAME} not in CONFIG_DB"

    def test_portchannel_admin_up(self, ssh):
        """PortChannel1 admin_status is 'up' in CONFIG_DB."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORTCHANNEL|{PORTCHANNEL_NAME}' admin_status",
            timeout=10,
        )
        assert out.strip() == "up", f"admin_status={out.strip()!r}"

    def test_portchannel_has_no_ip(self, ssh):
        """PortChannel1 has no IP address (L2 VLAN 999 access, SVI is on Vlan999)."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|{PORTCHANNEL_NAME}|*'",
            timeout=10,
        )
        assert not out.strip(), (
            f"PortChannel1 has IP configured; expected L2-only: {out.strip()}"
        )

    def test_portchannel_in_vlan999(self, ssh):
        """PortChannel1 is an untagged member of VLAN 999."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'VLAN_MEMBER|Vlan999|{PORTCHANNEL_NAME}' tagging_mode",
            timeout=10,
        )
        assert out.strip() == "untagged", (
            f"PortChannel1 VLAN 999 tagging_mode={out.strip()!r}; expected 'untagged'"
        )

    def test_vlan999_svi_ip(self, ssh):
        """Vlan999 SVI has 10.99.1.1/31."""
        out, _, _ = ssh.run(
            "redis-cli -n 4 keys 'VLAN_INTERFACE|Vlan999|*'", timeout=10
        )
        assert "10.99.1.1/31" in out, (
            f"Vlan999 SVI IP missing; expected 10.99.1.1/31. Keys: {out.strip()}"
        )

    def test_portchannel_members_in_config_db(self, ssh):
        """Both member ports are in PORTCHANNEL_MEMBER table."""
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 4 exists 'PORTCHANNEL_MEMBER|{PORTCHANNEL_NAME}|{port}'",
                timeout=10,
            )
            assert out.strip() == "1", f"{port} not a member of {PORTCHANNEL_NAME}"


# ------------------------------------------------------------------
# LACP negotiation state
# ------------------------------------------------------------------

class TestLACPState:

    def test_portchannel_lacp_active_up(self, ssh):
        """PortChannel1 shows LACP(A)(Up) in portchannel summary."""
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary
        protocol = summary[PORTCHANNEL_NAME]["protocol"]
        print(f"  {PORTCHANNEL_NAME} protocol: {protocol}")
        assert "Up" in protocol, f"protocol={protocol!r}; expected '(Up)'"

    def test_both_members_selected(self, ssh):
        """Both member ports show (S) = Selected in portchannel summary."""
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary
        members = summary[PORTCHANNEL_NAME]["members"]
        for port in LAG_MEMBERS:
            assert port in members, f"{port} not listed in {PORTCHANNEL_NAME} members"
            assert members[port] == "S", f"{port} state={members[port]!r}; expected 'S'"

    def test_teamdctl_state_current(self, ssh):
        """teamdctl reports both ports as 'state: current' (LACP converged)."""
        out, err, rc = ssh.run(f"teamdctl {PORTCHANNEL_NAME} state", timeout=15)
        assert rc == 0, f"teamdctl state failed: {err}"
        assert "state: current" in out, (
            f"Expected 'state: current' in teamdctl output\nOutput:\n{out[:500]}"
        )
        assert "active: yes" in out, "teamd runner is not active"


# ------------------------------------------------------------------
# APP_DB and STATE_DB propagation
# ------------------------------------------------------------------

class TestDBPropagation:

    def test_lag_table_in_app_db(self, ssh):
        """LAG_TABLE:PortChannel1 exists in APP_DB with oper_status=up."""
        out, _, _ = ssh.run(
            f"redis-cli -n 0 hget 'LAG_TABLE:{PORTCHANNEL_NAME}' oper_status",
            timeout=10,
        )
        assert out.strip() == "up", f"APP_DB LAG oper_status={out.strip()!r}"

    def test_lag_member_table_in_app_db(self, ssh):
        """LAG_MEMBER_TABLE entries exist in APP_DB for both members."""
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 0 hget 'LAG_MEMBER_TABLE:{PORTCHANNEL_NAME}:{port}' status",
                timeout=10,
            )
            assert out.strip() == "enabled", (
                f"LAG_MEMBER {port} status={out.strip()!r} in APP_DB"
            )


# ------------------------------------------------------------------
# ASIC_DB LAG objects
# ------------------------------------------------------------------

class TestASICDB:

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
        assert out.strip() == "1", f"SAI_OBJECT_TYPE_LAG:{oid} not in ASIC_DB"

    def test_sai_lag_member_objects_exist(self, ssh):
        """At least 2 SAI_OBJECT_TYPE_LAG_MEMBER entries in ASIC_DB."""
        out, _, _ = ssh.run(
            "redis-cli -n 1 keys 'ASIC_STATE:SAI_OBJECT_TYPE_LAG_MEMBER:*'",
            timeout=10,
        )
        members = [l for l in out.strip().splitlines() if l.strip()]
        assert len(members) >= 2, f"Expected >=2 LAG_MEMBER objects, found {len(members)}"


# ------------------------------------------------------------------
# L3 reachability over LAG — ping Vlan999 SVI
# ------------------------------------------------------------------

class TestLAGReachability:
    """Verify end-to-end reachability over PortChannel1 via Vlan999 SVI ping.

    This tests the full stack: LACP bundling → L2 forwarding through
    VLAN 999 → SVI → IP reachability to the EOS peer.
    """

    def test_ping_eos_over_lag(self, ssh):
        """SONiC can ping EOS Vlan999 SVI (10.99.1.0) over PortChannel1."""
        ok, out = _ping_peer(ssh, count=5, timeout_per=2)
        print(f"\n{out}")
        assert ok, (
            f"Cannot ping EOS ({EOS_SVI_IP}) over VLAN 999 LAG.\n"
            "Check: Vlan999 SVI IP on both sides, PortChannel1 in VLAN 999, "
            "LACP converged.\n"
            f"ping output:\n{out}"
        )

    def test_lldp_neighbor_on_lag_member(self, ssh):
        """LLDP neighbor (rabbit-lorax) is visible on Ethernet16 or Ethernet32."""
        out, _, rc = ssh.run("show lldp neighbors", timeout=30)
        assert rc == 0, f"show lldp neighbors failed: {out}"
        lag_lldp = [
            line for line in out.splitlines()
            if any(p in line for p in LAG_MEMBERS)
        ]
        assert lag_lldp, (
            "No LLDP neighbors found on Ethernet16 or Ethernet32.\n"
            "PortChannel1 is active but LLDP frames are not reaching the peer.\n"
            "Possible causes: LLDP container down, peer LLDP disabled."
        )
        print(f"  LLDP on LAG members: {len(lag_lldp)} entries found")


# ------------------------------------------------------------------
# LAG failover — ping-based convergence test
# ------------------------------------------------------------------

class TestLAGFailover:
    """Verify LAG survives a single member link failure.

    Phases:
      1. Baseline: ping EOS over LAG (both members up)
      2. Shut Ethernet16: ping must continue (Ethernet32 carries traffic)
      3. Restore Ethernet16: both members return to 'current', ping still works
    """

    @pytest.fixture(autouse=True)
    def _restore_lag_members(self, ssh):
        """Ensure all LAG member ports are admin-up after the test."""
        yield
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
            )
            if out.strip() != "up":
                ssh.run(f"sudo config interface startup {port}", timeout=15)
                time.sleep(2)
        # Wait for both members to reconverge
        deadline = time.time() + TEAMDCTL_RECOVER_TIMEOUT
        while time.time() < deadline:
            members = _teamdctl_members(ssh)
            if all(members.get(p) == "current" for p in LAG_MEMBERS):
                break
            time.sleep(TEAMDCTL_POLL_INTERVAL)

    def test_failover_and_recovery(self, ssh):
        """Shut Ethernet16 → ping survives; restore → both members reconverge."""
        fail_port = "Ethernet16"
        survive_port = "Ethernet32"

        # Baseline: ping works
        ok, out = _ping_peer(ssh, count=2, timeout_per=2)
        assert ok, f"Baseline ping failed before failover test:\n{out}"

        # Phase 1: shut down fail_port
        _, _, rc = ssh.run(f"sudo config interface shutdown {fail_port}", timeout=15)
        assert rc == 0, f"Failed to shutdown {fail_port}"

        # Wait for survive_port to remain 'current' in teamdctl
        ok = _wait_for_member_state(ssh, survive_port, "current", TEAMDCTL_FAILOVER_TIMEOUT)
        members = _teamdctl_members(ssh)
        print(f"  After shutdown {fail_port}: teamdctl members={members}")
        assert ok, (
            f"{survive_port} did not remain 'current' within {TEAMDCTL_FAILOVER_TIMEOUT}s "
            f"after shutting {fail_port}. Members: {members}"
        )

        # PortChannel still up
        summary = _portchannel_summary(ssh)
        assert "Up" in summary.get(PORTCHANNEL_NAME, {}).get("protocol", ""), (
            f"{PORTCHANNEL_NAME} went down after shutting {fail_port}"
        )

        # Ping survives on remaining member
        ok, out = _ping_peer(ssh, count=3, timeout_per=2)
        print(f"  Ping during failover (1 member down):\n{out}")
        assert ok, (
            f"Ping failed with {fail_port} down — LAG should forward via {survive_port}.\n"
            f"ping output:\n{out}"
        )

        # Phase 2: restore fail_port
        _, _, rc = ssh.run(f"sudo config interface startup {fail_port}", timeout=15)
        assert rc == 0, f"Failed to startup {fail_port}"

        # Wait for both members to return to 'current'
        deadline = time.time() + TEAMDCTL_RECOVER_TIMEOUT
        both_selected = False
        while time.time() < deadline:
            members = _teamdctl_members(ssh)
            if all(members.get(p) == "current" for p in LAG_MEMBERS):
                both_selected = True
                break
            time.sleep(TEAMDCTL_POLL_INTERVAL)

        members = _teamdctl_members(ssh)
        print(f"  After recovery: teamdctl members={members}")
        assert both_selected, (
            f"Both members did not return to 'current' within {TEAMDCTL_RECOVER_TIMEOUT}s. "
            f"Members: {members}"
        )

        # Ping still works after recovery
        ok, out = _ping_peer(ssh, count=3, timeout_per=2)
        assert ok, f"Ping failed after member recovery:\n{out}"


# ------------------------------------------------------------------
# Standalone ports unaffected
# ------------------------------------------------------------------

class TestStandalonePortsUnaffected:

    def test_standalone_ports_still_up(self, ssh):
        """Ethernet48 and Ethernet112 (not in LAG) remain oper=up."""
        out, _, rc = ssh.run("show interfaces status", timeout=30)
        assert rc == 0
        for port in STANDALONE_PORTS:
            m = re.search(rf"\s*{port}\s+.*?\s+(up|down)\s+(up|down)", out)
            assert m, f"Could not parse status for {port}"
            oper = m.group(1)
            print(f"  {port}: oper={oper}")
            assert oper == "up", f"{port} oper={oper!r} — standalone port should be up"
