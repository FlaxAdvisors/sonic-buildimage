"""Stage 23 — Throughput verification via iperf3.

Tests:
  test_throughput_10g              Ethernet66 ↔ Ethernet67  ≥ 8 Gbps
  test_throughput_25g_pair1        Ethernet80 ↔ Ethernet81  ≥ 20 Gbps
  test_throughput_25g_pair2        Ethernet0  ↔ Ethernet1   ≥ 20 Gbps (skip if Ethernet1 dark)
  test_throughput_cross_qsfp       Ethernet66 ↔ Ethernet80  ≥ 8 Gbps  (bottleneck 10G)
  test_throughput_100g_eth48       Ethernet48 ↔ EOS Et15/1  ≥ 90 Gbps
  test_throughput_100g_eth112      Ethernet112 ↔ EOS Et16/1 ≥ 90 Gbps

All tests skip (not fail) when: iperf3 absent, host SSH unreachable, EOS iperf3 absent.
"""

import json
import os
import pytest

# EOS SSH coordinates — direct, no jump host needed when Po1 carries no IP.
EOS_HOST    = "192.168.88.14"
EOS_USER    = "admin"
EOS_PASSWD  = "0penSesame"

# SONiC switch SSH — we run iperf3 client from the switch side for 100G tests.
SONIC_HOST  = "192.168.88.12"
SONIC_USER  = "admin"

# Temporary /30 subnet for 100G switch-to-switch tests.
SONIC_TEMP_IP_ETH48   = "10.99.48.1/30"
EOS_TEMP_IP_ETH48     = "10.99.48.2"
SONIC_TEMP_IP_ETH112  = "10.99.112.1/30"
EOS_TEMP_IP_ETH112    = "10.99.112.2"

# Thresholds in bits/second
THRESH_10G  = 8e9
THRESH_25G  = 20e9
THRESH_100G = 90e9

# iperf3 test duration seconds
IPERF_DURATION = 10


def _host_ssh(mgmt_ip, creds):
    """Return a connected paramiko SSHClient or raise on failure."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": mgmt_ip, "username": creds["ssh_user"], "timeout": 10}
    kf = creds.get("key_file")
    if kf:
        kw["key_filename"] = os.path.expanduser(kf)
    client.connect(**kw)
    return client


def _run(client, cmd, timeout=30):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    return out, err, rc


def _host_reachable(mgmt_ip, creds):
    try:
        c = _host_ssh(mgmt_ip, creds)
        c.close()
        return True
    except Exception:
        return False


def _iperf3_on_host(mgmt_ip, creds):
    try:
        c = _host_ssh(mgmt_ip, creds)
        out, _, rc = _run(c, "which iperf3 2>/dev/null; echo exit:$?")
        c.close()
        return "exit:0" in out
    except Exception:
        return False


def _run_iperf3_pair(server_ip, server_mgmt, client_mgmt, creds, threshold):
    """Start iperf3 server on server_mgmt, run client from client_mgmt to server_ip.

    Returns bits_per_second (float) from client sum_received.
    Raises AssertionError if throughput < threshold.
    """
    srv = _host_ssh(server_mgmt, creds)
    cli = _host_ssh(client_mgmt, creds)
    try:
        # Kill any stale iperf3 server
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        # Start server in background
        _run(srv, "nohup iperf3 -s -1 -D 2>/dev/null &", timeout=5)
        import time; time.sleep(1)
        # Run client
        out, err, rc = _run(cli,
            f"iperf3 -c {server_ip} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15)
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= threshold, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {threshold/1e9:.0f} Gbps"
        )
        return bps
    finally:
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        srv.close()
        cli.close()


# ── Host-to-host tests ──────────────────────────────────────────────────────

def test_throughput_10g(host_by_port, host_ssh_creds):
    """Ethernet66 ↔ Ethernet67 via VLAN 10; threshold ≥ 8 Gbps."""
    h66 = host_by_port.get("Ethernet66")
    h67 = host_by_port.get("Ethernet67")
    if not h66 or not h67:
        pytest.skip("Ethernet66 or Ethernet67 not in topology.json")

    if not _host_reachable(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h66['mgmt_ip']} (port Ethernet66) not reachable via SSH")
    if not _host_reachable(h67["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h67['mgmt_ip']} (port Ethernet66) not reachable via SSH")
    if not _iperf3_on_host(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h66['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h67["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h67['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h66["test_ip"], h66["mgmt_ip"], h67["mgmt_ip"],
                            host_ssh_creds, THRESH_10G)
    print(f"\n  10G pair throughput: {bps/1e9:.2f} Gbps")


def test_throughput_25g_pair1(host_by_port, host_ssh_creds):
    """Ethernet80 ↔ Ethernet81 via VLAN 10; threshold ≥ 20 Gbps."""
    h80 = host_by_port.get("Ethernet80")
    h81 = host_by_port.get("Ethernet81")
    if not h80 or not h81:
        pytest.skip("Ethernet80 or Ethernet81 not in topology.json")

    if not _host_reachable(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h80['mgmt_ip']} (port Ethernet80) not reachable via SSH")
    if not _host_reachable(h81["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h81['mgmt_ip']} (port Ethernet81) not reachable via SSH")
    if not _iperf3_on_host(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h80['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h81["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h81['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h80["test_ip"], h80["mgmt_ip"], h81["mgmt_ip"],
                            host_ssh_creds, THRESH_25G)
    print(f"\n  25G pair1 throughput: {bps/1e9:.2f} Gbps")


def test_throughput_25g_pair2(host_by_port, host_ssh_creds):
    """Ethernet0 ↔ Ethernet1 via VLAN 10; threshold ≥ 20 Gbps.

    EXPECTED SKIP: Ethernet1 is a confirmed dark lane (see TODO.md).
    """
    h0 = host_by_port.get("Ethernet0")
    h1 = host_by_port.get("Ethernet1")
    if not h0 or not h1:
        pytest.skip("Ethernet0 or Ethernet1 not in topology.json")

    if not _host_reachable(h1["mgmt_ip"], host_ssh_creds):
        pytest.skip(
            f"Ethernet1 dark lane (see TODO.md) — test_throughput_25g_pair2 skipped"
        )
    if not _host_reachable(h0["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h0['mgmt_ip']} (port Ethernet0) not reachable via SSH")
    if not _iperf3_on_host(h0["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h0['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h1["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h1['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h0["test_ip"], h0["mgmt_ip"], h1["mgmt_ip"],
                            host_ssh_creds, THRESH_25G)
    print(f"\n  25G pair2 throughput: {bps/1e9:.2f} Gbps")


def test_throughput_cross_qsfp(host_by_port, host_ssh_creds):
    """Ethernet66 (10G) ↔ Ethernet80 (25G) cross-QSFP via VLAN 10; threshold ≥ 8 Gbps."""
    h66 = host_by_port.get("Ethernet66")
    h80 = host_by_port.get("Ethernet80")
    if not h66 or not h80:
        pytest.skip("Ethernet66 or Ethernet80 not in topology.json")

    if not _host_reachable(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h66['mgmt_ip']} (port Ethernet66) not reachable via SSH")
    if not _host_reachable(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h80['mgmt_ip']} (port Ethernet80) not reachable via SSH")
    if not _iperf3_on_host(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h66['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h80['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h66["test_ip"], h66["mgmt_ip"], h80["mgmt_ip"],
                            host_ssh_creds, THRESH_10G)
    print(f"\n  Cross-QSFP throughput: {bps/1e9:.2f} Gbps")


# ── 100G switch-to-switch tests ─────────────────────────────────────────────

def _eos_ssh():
    """Return a connected paramiko SSHClient to EOS."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(EOS_HOST, username=EOS_USER, password=EOS_PASSWD, timeout=10)
    return client


def _sonic_ssh():
    """Return a connected paramiko SSHClient to SONiC switch."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SONIC_HOST, username=SONIC_USER, timeout=10)
    return client


def _iperf3_on_eos():
    try:
        c = _eos_ssh()
        out, _, rc = _run(c, "bash -c 'which iperf3 2>/dev/null; echo exit:$?'")
        c.close()
        return "exit:0" in out
    except Exception:
        return False


@pytest.fixture
def sonic_eth48_temp_ip(ssh):
    """Assign and teardown temp /30 IP on Ethernet48 for 100G test."""
    ssh.run(f"sudo config interface ip add Ethernet48 {SONIC_TEMP_IP_ETH48}", timeout=10)
    yield SONIC_TEMP_IP_ETH48.split('/')[0]
    ssh.run(f"sudo config interface ip remove Ethernet48 {SONIC_TEMP_IP_ETH48}", timeout=10)


@pytest.fixture
def eos_eth_temp_ip_48():
    """Assign and teardown temp IP on EOS Et15/1 for 100G test."""
    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'ip addr add {EOS_TEMP_IP_ETH48}/30 dev et15 2>/dev/null || true'")
        yield EOS_TEMP_IP_ETH48
    finally:
        _run(eos, f"bash -c 'ip addr del {EOS_TEMP_IP_ETH48}/30 dev et15 2>/dev/null || true'")
        eos.close()


@pytest.fixture
def sonic_eth112_temp_ip(ssh):
    """Assign and teardown temp /30 IP on Ethernet112 for 100G test."""
    ssh.run(f"sudo config interface ip add Ethernet112 {SONIC_TEMP_IP_ETH112}", timeout=10)
    yield SONIC_TEMP_IP_ETH112.split('/')[0]
    ssh.run(f"sudo config interface ip remove Ethernet112 {SONIC_TEMP_IP_ETH112}", timeout=10)


@pytest.fixture
def eos_eth_temp_ip_112():
    """Assign and teardown temp IP on EOS Et16/1 for 100G test."""
    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'ip addr add {EOS_TEMP_IP_ETH112}/30 dev et16 2>/dev/null || true'")
        yield EOS_TEMP_IP_ETH112
    finally:
        _run(eos, f"bash -c 'ip addr del {EOS_TEMP_IP_ETH112}/30 dev et16 2>/dev/null || true'")
        eos.close()


def test_throughput_100g_eth48(ssh, sonic_eth48_temp_ip, eos_eth_temp_ip_48):
    """Ethernet48 ↔ EOS Et15/1 at 100G; threshold ≥ 90 Gbps."""
    if not _iperf3_on_eos():
        pytest.skip("iperf3 not found in EOS bash — cannot run 100G switch-to-switch test")

    eos = _eos_ssh()
    try:
        # Kill stale, start server on EOS
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null; nohup iperf3 -s -1 -D 2>/dev/null &'")
        import time; time.sleep(1)

        # Run iperf3 client from SONiC switch toward EOS
        out, err, rc = ssh.run(
            f"iperf3 -c {eos_eth_temp_ip_48} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15
        )
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= THRESH_100G, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {THRESH_100G/1e9:.0f} Gbps"
        )
        print(f"\n  Ethernet48↔EOS throughput: {bps/1e9:.2f} Gbps")
    finally:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null || true'")
        eos.close()


def test_throughput_100g_eth112(ssh, sonic_eth112_temp_ip, eos_eth_temp_ip_112):
    """Ethernet112 ↔ EOS Et16/1 at 100G; threshold ≥ 90 Gbps."""
    if not _iperf3_on_eos():
        pytest.skip("iperf3 not found in EOS bash — cannot run 100G switch-to-switch test")

    eos = _eos_ssh()
    try:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null; nohup iperf3 -s -1 -D 2>/dev/null &'")
        import time; time.sleep(1)

        out, err, rc = ssh.run(
            f"iperf3 -c {eos_eth_temp_ip_112} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15
        )
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= THRESH_100G, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {THRESH_100G/1e9:.0f} Gbps"
        )
        print(f"\n  Ethernet112↔EOS throughput: {bps/1e9:.2f} Gbps")
    finally:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null || true'")
        eos.close()
