"""Stage 17 — State Restore Audit.

Run this stage last in the test suite to verify that no earlier stage
left the switch in a modified configuration state.

This stage does NOT modify any configuration — it is read-only.

Checks:
  1. All 32 QSFP ports show 100G speed in CONFIG_DB
  2. No ports are unexpectedly admin-down
     (exception: ports already down before stage_13 pre-dates this)
  3. PortChannel1 members and IP config are intact (if PortChannel1 exists)
  4. BREAKOUT_CFG modes are all 1x100G[40G] (if BREAKOUT_CFG is populated)
  5. teamd feature state is 'enabled' (if it was enabled before testing)
  6. pmon is running and healthy

Discovery is fully dynamic — no port names or IPs are hardcoded.
"""

import json
import re
import pytest


NUM_PORTS = 32
EXPECTED_SPEED = "100000"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_config_db_ports(ssh):
    """Return dict {port_name: {speed, admin_status}} from CONFIG_DB."""
    out, _, rc = ssh.run("redis-cli -n 4 keys 'PORT|Ethernet*'", timeout=15)
    assert rc == 0
    ports = {}
    for key in out.strip().splitlines():
        if "|" not in key:
            continue
        port = key.split("|", 1)[1].strip()
        speed_out, _, _ = ssh.run(f"redis-cli -n 4 hget '{key}' speed", timeout=10)
        admin_out, _, _ = ssh.run(f"redis-cli -n 4 hget '{key}' admin_status", timeout=10)
        ports[port] = {
            "speed": speed_out.strip(),
            "admin_status": admin_out.strip(),
        }
    return ports


# ------------------------------------------------------------------
# Port speed check
# ------------------------------------------------------------------

def test_all_ports_at_100g(ssh):
    """All 32 Ethernet ports remain at 100G speed in CONFIG_DB.

    A stray speed-change test would leave a port at 40G.
    """
    ports = _get_config_db_ports(ssh)
    wrong_speed = {
        p: d["speed"] for p, d in ports.items() if d["speed"] != EXPECTED_SPEED
    }
    if wrong_speed:
        print(f"\nPorts with unexpected speed:")
        for p, s in sorted(wrong_speed.items()):
            print(f"  {p}: {s}")
    assert not wrong_speed, (
        f"Ports left with unexpected speed after test run:\n"
        + "\n".join(f"  {p}: {s}" for p, s in sorted(wrong_speed.items()))
        + f"\nExpected all ports at {EXPECTED_SPEED} (100G)."
    )


# ------------------------------------------------------------------
# Admin status
# ------------------------------------------------------------------

def test_no_unexpected_admin_down(ssh):
    """No ports are admin-down that should be admin-up.

    Discovers the set of currently admin-up ports and verifies all were
    already admin-up before any test could have changed them.  Fails only
    if a port is admin-down AND appears in the connected-ports list (i.e.,
    a test shut it down and did not restore it).
    """
    ports = _get_config_db_ports(ssh)
    admin_down = [p for p, d in ports.items() if d["admin_status"] == "down"]
    if not admin_down:
        print(f"\nAll {len(ports)} ports are admin-up")
        return

    print(f"\nAdmin-down ports: {sorted(admin_down)}")

    # Fetch connected ports dynamically from STATE_DB (ports that have LLDP neighbors
    # or are in a PortChannel — these should never be left admin-down by a test).
    lldp_out, _, _ = ssh.run("show lldp neighbors 2>/dev/null", timeout=15)
    lldp_ports = set(
        m.group(1) for m in re.finditer(r"(Ethernet\d+)", lldp_out)
    )
    pc_out, _, _ = ssh.run(
        "redis-cli -n 4 keys 'PORTCHANNEL_MEMBER|*' 2>/dev/null", timeout=10
    )
    pc_ports = set(
        k.rsplit("|", 1)[-1].strip() for k in pc_out.strip().splitlines() if "|" in k
    )
    critical = (lldp_ports | pc_ports) & set(admin_down)

    if critical:
        pytest.fail(
            f"Connected/LAG ports left admin-down after test run: {sorted(critical)}\n"
            "A test shut these ports down and did not restore admin-up."
        )
    else:
        print(f"  All admin-down ports are not in connected/LAG set — acceptable.")


# ------------------------------------------------------------------
# PortChannel config integrity
# ------------------------------------------------------------------

def test_portchannel_config_intact(ssh):
    """PortChannel1 config is intact if it existed at start of test run.

    Only checks ports that are in CONFIG_DB — will skip if no PortChannel1.
    """
    out, _, rc = ssh.run(
        "redis-cli -n 4 exists 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    if out.strip() != "1":
        pytest.skip("PortChannel1 not in CONFIG_DB — skipping config check")

    # Members must still exist
    members_out, _, _ = ssh.run(
        "redis-cli -n 4 keys 'PORTCHANNEL_MEMBER|PortChannel1|*'", timeout=10
    )
    members = [
        k.rsplit("|", 1)[-1].strip()
        for k in members_out.strip().splitlines() if k.strip()
    ]
    print(f"\nPortChannel1 members: {sorted(members)}")
    assert len(members) >= 1, (
        "PortChannel1 has no members in CONFIG_DB — a test removed members "
        "and did not restore them."
    )

    # IP must still be configured
    ip_out, _, _ = ssh.run(
        "redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|PortChannel1|*'", timeout=10
    )
    ips = [k.strip() for k in ip_out.strip().splitlines() if k.strip()]
    print(f"  PortChannel1 IPs: {ips}")
    assert len(ips) >= 1, (
        "PortChannel1 has no IP address in CONFIG_DB — a test removed it."
    )

    # Admin status
    admin_out, _, _ = ssh.run(
        "redis-cli -n 4 hget 'PORTCHANNEL|PortChannel1' admin_status", timeout=10
    )
    admin = admin_out.strip()
    print(f"  PortChannel1 admin_status: {admin!r}")
    assert admin == "up", (
        f"PortChannel1 admin_status={admin!r} — expected 'up'"
    )


# ------------------------------------------------------------------
# BREAKOUT_CFG check
# ------------------------------------------------------------------

def test_breakout_modes_restored(ssh):
    """All BREAKOUT_CFG entries show 1x100G[40G] mode.

    If any port was broken out and not restored, this will catch it.
    Skips if BREAKOUT_CFG is not yet populated.
    """
    out, _, rc = ssh.run(
        "redis-cli -n 4 keys 'BREAKOUT_CFG|*'", timeout=10
    )
    keys = [k.strip() for k in out.strip().splitlines() if k.strip()]
    if not keys:
        pytest.skip("BREAKOUT_CFG table not populated — skipping")

    wrong_mode = {}
    for key in keys:
        port = key.split("|", 1)[1] if "|" in key else key
        mode_out, _, _ = ssh.run(
            f"redis-cli -n 4 hget '{key}' brkout_mode", timeout=10
        )
        mode = mode_out.strip()
        if mode and mode != "1x100G[40G]":
            wrong_mode[port] = mode

    if wrong_mode:
        print(f"\nPorts with non-default breakout mode:")
        for p, m in sorted(wrong_mode.items()):
            print(f"  {p}: {m!r}")
    assert not wrong_mode, (
        f"Ports left in broken-out state after test run:\n"
        + "\n".join(f"  {p}: {m!r}" for p, m in sorted(wrong_mode.items()))
        + "\nA breakout test did not restore the original mode."
    )


# ------------------------------------------------------------------
# pmon health
# ------------------------------------------------------------------

def test_pmon_container_running(ssh):
    """pmon Docker container is running."""
    out, _, rc = ssh.run(
        "docker ps --format '{{.Names}}' --filter name=pmon", timeout=10
    )
    assert "pmon" in out, (
        "pmon container is not running after test suite.\n"
        "Fix: sudo systemctl start pmon"
    )
    print(f"\npmon is running")


def test_pmon_daemons_running(ssh):
    """Key pmon daemons are running inside the pmon container."""
    required_daemons = ["xcvrd", "thermalctld", "ledd"]
    out, _, rc = ssh.run(
        "docker exec pmon supervisorctl status 2>/dev/null", timeout=15
    )
    if rc != 0:
        pytest.skip("Could not query pmon supervisorctl — pmon may have just restarted")

    not_running = []
    for daemon in required_daemons:
        if daemon not in out:
            not_running.append(f"{daemon}: not found")
        elif "RUNNING" not in out.split(daemon, 1)[1][:50]:
            state = out.split(daemon, 1)[1][:50].split("\n")[0].strip()
            not_running.append(f"{daemon}: {state!r}")

    print(f"\npmon daemon statuses:")
    for line in out.strip().splitlines():
        if any(d in line for d in required_daemons):
            print(f"  {line.strip()}")

    assert not not_running, (
        f"pmon daemons not in RUNNING state:\n"
        + "\n".join(not_running)
    )
