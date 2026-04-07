"""Stage 23 prerequisites — host reachability, iperf3, and test-network connectivity.

These tests run before test_throughput.py (alphabetical order) and give clear
failures when the environment isn't ready.  The entire module is skipped when
topology.json has no hosts configured.

Checks per host:
  test_host_ssh_reachable        mgmt SSH works
  test_host_iperf3_installed     iperf3 binary present
  test_host_test_ip_configured   10.0.10.x assigned on the switch-facing NIC

Checks per throughput pair:
  test_pair_test_network_reachable   host A can ping host B on 10.0.10.x via switch
"""

import json
import os
import pytest

# ---------------------------------------------------------------------------
# Load topology at module level for parametrize
# ---------------------------------------------------------------------------

_TOPO_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'tools', 'topology.json')
with open(_TOPO_PATH) as _f:
    _TOPO = json.load(_f)

_HOSTS = _TOPO.get('hosts', [])  # list of {port, mgmt_ip, test_ip}

# Pairs as defined in test_throughput.py (port_a, port_b).
# Round 1 — same-speed:
#   Ethernet24 ↔ Ethernet28  (100G↔100G)
#   Ethernet20 ↔ Ethernet22  ( 50G↔ 50G)
#   Ethernet0  ↔ Ethernet80  ( 25G↔ 25G)
#   Ethernet66 ↔ Ethernet67  ( 10G↔ 10G)
# Round 2 — cross-speed:
#   Ethernet24 ↔ Ethernet20  (100G↔50G)
#   Ethernet28 ↔ Ethernet66  (100G↔10G)
#   Ethernet22 ↔ Ethernet80  ( 50G↔25G)
_PAIRS = [
    ("Ethernet24", "Ethernet28"),   # 100G↔100G
    ("Ethernet20", "Ethernet22"),   # 50G↔50G
    ("Ethernet0",  "Ethernet80"),   # 25G↔25G
    ("Ethernet66", "Ethernet67"),   # 10G↔10G
    ("Ethernet24", "Ethernet20"),   # 100G↔50G
    ("Ethernet28", "Ethernet66"),   # 100G↔10G
    ("Ethernet22", "Ethernet80"),   # 50G↔25G
]

pytestmark = pytest.mark.skipif(
    not _HOSTS,
    reason="No hosts in topology.json — stage_23 host-to-host tests not configured"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ssh_run(mgmt_ip, creds, cmd, timeout=15):
    """SSH to mgmt_ip and run cmd; return (stdout, stderr, rc)."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": mgmt_ip, "username": creds["ssh_user"], "timeout": 10}
    if creds.get("key_file"):
        kw["key_filename"] = os.path.expanduser(creds["key_file"])
    client.connect(**kw)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    client.close()
    return out, err, rc


def _host_by_port(port):
    return next((h for h in _HOSTS if h["port"] == port), None)


# ---------------------------------------------------------------------------
# Per-host: SSH reachability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", _HOSTS, ids=[h["port"] for h in _HOSTS])
def test_host_ssh_reachable(host, host_ssh_creds):
    """Each topology host must be reachable via SSH on its mgmt_ip."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": host["mgmt_ip"], "username": host_ssh_creds["ssh_user"], "timeout": 10}
    if host_ssh_creds.get("key_file"):
        kw["key_filename"] = os.path.expanduser(host_ssh_creds["key_file"])
    try:
        client.connect(**kw)
        out, _, rc = _exec(client, "hostname")
        client.close()
        print(f"\n  {host['port']} ({host['mgmt_ip']}): SSH OK — hostname={out.strip()!r}")
    except Exception as exc:
        pytest.fail(
            f"{host['port']} ({host['mgmt_ip']}): SSH unreachable — {exc}\n"
            "Check that the host is powered on and sshd is running."
        )


def _exec(client, cmd, timeout=15):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    return out, err, rc


# ---------------------------------------------------------------------------
# Per-host: iperf3 installed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", _HOSTS, ids=[h["port"] for h in _HOSTS])
def test_host_iperf3_installed(host, host_ssh_creds):
    """iperf3 must be present on each topology host — installs it if missing."""
    try:
        out, err, rc = _ssh_run(host["mgmt_ip"], host_ssh_creds, "which iperf3 2>/dev/null; echo exit:$?")
    except Exception as exc:
        pytest.skip(f"SSH to {host['mgmt_ip']} failed ({exc}) — run test_host_ssh_reachable first")

    if "exit:0" not in out:
        print(f"\n  {host['port']} ({host['mgmt_ip']}): iperf3 missing — installing...")
        # Use OS-level timeout so a slow mirror can't hang the test suite.
        # Cleanup stale dpkg locks first in case a prior interrupted install left them.
        _ssh_run(
            host["mgmt_ip"], host_ssh_creds,
            "sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock "
            "/var/cache/apt/archives/lock 2>/dev/null; "
            "sudo dpkg --configure -a 2>/dev/null; true",
            timeout=30,
        )
        _ssh_run(
            host["mgmt_ip"], host_ssh_creds,
            "sudo DEBIAN_FRONTEND=noninteractive timeout 120 apt-get install -y iperf3 2>&1; true",
            timeout=150,
        )
        # Verify installation succeeded regardless of apt-get exit code
        out3, _, rc3 = _ssh_run(
            host["mgmt_ip"], host_ssh_creds,
            "which iperf3 2>/dev/null; echo exit:$?",
        )
        assert "exit:0" in out3, (
            f"{host['port']} ({host['mgmt_ip']}): iperf3 still not found after install attempt.\n"
            "Check apt-get connectivity on the host."
        )
        print(f"  installed OK")
    else:
        iperf_path = out.strip().splitlines()[0]
        print(f"\n  {host['port']} ({host['mgmt_ip']}): iperf3 at {iperf_path}")


# ---------------------------------------------------------------------------
# Per-host: test_ip configured on switch-facing NIC
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", _HOSTS, ids=[h["port"] for h in _HOSTS])
def test_host_test_ip_configured(host, host_ssh_creds):
    """Each host must have its test_ip (10.0.10.x) assigned on a local interface."""
    test_ip = host["test_ip"]
    try:
        out, err, rc = _ssh_run(
            host["mgmt_ip"], host_ssh_creds,
            f"ip -4 addr show | grep -w '{test_ip}' | awk '{{print $NF, $2}}'",
        )
    except Exception as exc:
        pytest.skip(f"SSH to {host['mgmt_ip']} failed ({exc}) — run test_host_ssh_reachable first")

    assert out.strip(), (
        f"{host['port']} ({host['mgmt_ip']}): test_ip {test_ip} is not configured "
        f"on any interface.\n"
        f"Assign it to the switch-facing NIC, e.g.:\n"
        f"  sudo ip addr add {test_ip}/24 dev ens1f1np1"
    )
    # Show which interface it's on
    iface_info = out.strip().splitlines()[0]
    print(f"\n  {host['port']} ({host['mgmt_ip']}): {test_ip} on {iface_info}")


# ---------------------------------------------------------------------------
# Per-pair: test-network reachability over 10.0.10.x (through the switch)
# ---------------------------------------------------------------------------

def _pair_params():
    """Build parametrize list for pairs where both hosts exist in topology."""
    params = []
    for port_a, port_b in _PAIRS:
        ha = _host_by_port(port_a)
        hb = _host_by_port(port_b)
        if ha and hb:
            params.append(pytest.param(ha, hb, id=f"{port_a}→{port_b}"))
    return params


@pytest.mark.parametrize("host_a,host_b", _pair_params())
def test_pair_test_network_reachable(host_a, host_b, host_ssh_creds):
    """host_a must be able to ping host_b's test_ip across the switch VLAN."""
    try:
        out, err, rc = _ssh_run(
            host_a["mgmt_ip"], host_ssh_creds,
            f"ping -c3 -W2 -I {host_a['test_ip']} {host_b['test_ip']} 2>&1",
            timeout=20,
        )
    except Exception as exc:
        pytest.skip(f"SSH to {host_a['mgmt_ip']} failed ({exc})")

    print(f"\n  {host_a['port']} ({host_a['test_ip']}) → "
          f"{host_b['port']} ({host_b['test_ip']}):\n{out.strip()}")

    assert rc == 0, (
        f"{host_a['port']} cannot reach {host_b['port']} on {host_b['test_ip']} "
        f"via the switch VLAN 10.\n"
        f"Check: test_ip assigned on both hosts, both ports in VLAN 10, switch forwarding.\n"
        f"ping output:\n{out}"
    )
