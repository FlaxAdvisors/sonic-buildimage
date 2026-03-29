"""Stage 23 — Throughput verification via iperf3.

Tests:
  test_throughput_round1        Round 1 parallel:
                                  Ethernet0  ↔ Ethernet80  ≥ 20 Gbps  (25G, cross-QSFP)
                                  Ethernet66 ↔ Ethernet67  ≥  8 Gbps  (10G, same QSFP)
  test_throughput_round2        Round 2 parallel:
                                  Ethernet80 ↔ Ethernet81  ≥ 20 Gbps  (25G, same QSFP)
                                  Ethernet66 ↔ Ethernet0   ≥  8 Gbps  (10G↔25G, cross-QSFP)
  test_throughput_100g_eth48    Ethernet48  ↔ EOS Et15/1  ≥ 90 Gbps
  test_throughput_100g_eth112   Ethernet112 ↔ EOS Et16/1  ≥ 90 Gbps

Each test pair runs simultaneously within a round; no host is reused within a round.

All tests skip (not fail) when: iperf3 absent, host SSH unreachable, EOS iperf3 absent.

Counter instrumentation: each test captures SAI COUNTERS_DB before/after iperf and
prints byte deltas, providing hardware-verified evidence that ASIC counters track iperf
traffic.  Counter reads are best-effort — failures are printed, not asserted.

iperf3 binding: both server (-B server_test_ip) and client (-B client_test_ip) are
explicitly bound to their test IPs so traffic routes through the switch and never over
the 192.168.88.x lab management network.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

# EOS SSH coordinates — direct, no jump host needed when Po1 carries no IP.
EOS_HOST    = "192.168.88.14"
EOS_USER    = "admin"
EOS_PASSWD  = "0penSesame"

# Temporary /30 subnet for 100G switch-to-switch tests.
SONIC_TEMP_IP_ETH48   = "10.99.48.1/30"
EOS_TEMP_IP_ETH48     = "10.99.48.2"
SONIC_TEMP_IP_ETH112  = "10.99.112.1/30"
EOS_TEMP_IP_ETH112    = "10.99.112.2"

# Thresholds in bits/second
THRESH_10G  = 8e9
THRESH_25G  = 20e9
THRESH_100G = 90e9

# iperf3 test duration seconds — 30s gives TCP time to reach steady-state throughput
IPERF_DURATION = 30


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


def _counters_snapshot(ssh, ports):
    """Read SAI_PORT_STAT_IF_IN/OUT_OCTETS for named ports from COUNTERS_DB.

    Returns {port: (in_octets, out_octets)}.  Missing ports are omitted silently.
    """
    snap = {}
    for port in ports:
        try:
            out, _, rc = ssh.run(
                f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10
            )
            if rc != 0 or not out.strip():
                continue
            oid = out.strip()
            out2, _, _ = ssh.run(
                f"redis-cli -n 2 hmget 'COUNTERS:{oid}' "
                "SAI_PORT_STAT_IF_IN_OCTETS SAI_PORT_STAT_IF_OUT_OCTETS",
                timeout=10,
            )
            vals = [v.strip() for v in out2.strip().splitlines()]
            snap[port] = (
                int(vals[0]) if vals and vals[0].isdigit() else 0,
                int(vals[1]) if len(vals) > 1 and vals[1].isdigit() else 0,
            )
        except Exception:
            pass
    return snap


def _print_counter_delta(before, after):
    """Print per-port octet delta between two snapshots."""
    for port in sorted(set(before) & set(after)):
        din  = after[port][0] - before[port][0]
        dout = after[port][1] - before[port][1]
        print(f"    {port}: ASIC ΔRX={din/1e9:.3f} GB  ΔTX={dout/1e9:.3f} GB")


def _run_iperf3_pair(label, server_test_ip, server_mgmt,
                     client_test_ip, client_mgmt, creds, threshold):
    """Start iperf3 server on server_mgmt bound to server_test_ip,
    run client from client_mgmt bound to client_test_ip.

    Both endpoints bind to their 10.0.10.x address (-B flag) so traffic
    routes through the switch VLAN 10, not the 192.168.88.x mgmt network.

    Returns (label, bits_per_second).
    Raises AssertionError if throughput < threshold.
    """
    srv = _host_ssh(server_mgmt, creds)
    cli = _host_ssh(client_mgmt, creds)
    try:
        # Kill any stale iperf3 server
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        # Start server bound to test_ip (switch-facing NIC)
        _run(srv, f"nohup iperf3 -s -1 -B {server_test_ip} -D 2>/dev/null &", timeout=5)
        time.sleep(1)
        # Run client bound to its own test_ip, connecting to server's test_ip
        out, err, rc = _run(cli,
            f"iperf3 -c {server_test_ip} -B {client_test_ip} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15)
        assert rc == 0, f"[{label}] iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= threshold, (
            f"[{label}] Throughput {bps/1e9:.2f} Gbps < threshold {threshold/1e9:.0f} Gbps"
        )
        return label, bps
    finally:
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        srv.close()
        cli.close()


def _require_hosts(host_by_port, host_ssh_creds, *port_names):
    """Skip if any port has no topology entry, host unreachable, or no iperf3."""
    hosts = {}
    for port in port_names:
        h = host_by_port.get(port)
        if not h:
            pytest.skip(f"{port} not in topology.json")
        if not _host_reachable(h["mgmt_ip"], host_ssh_creds):
            pytest.skip(f"Host {h['mgmt_ip']} ({port}) not reachable via SSH")
        if not _iperf3_on_host(h["mgmt_ip"], host_ssh_creds):
            pytest.skip(f"iperf3 not found on host {h['mgmt_ip']} ({port})")
        hosts[port] = h
    return hosts


# ── Host-to-host parallel round tests ───────────────────────────────────────

def test_throughput_round1(ssh, host_by_port, host_ssh_creds):
    """Round 1 — two pairs run simultaneously:
      Ethernet0  ↔ Ethernet80  (25G, cross-QSFP) ≥ 20 Gbps
      Ethernet66 ↔ Ethernet67  (10G, same QSFP)  ≥  8 Gbps
    """
    hosts = _require_hosts(host_by_port, host_ssh_creds,
                           "Ethernet0", "Ethernet80", "Ethernet66", "Ethernet67")
    h0, h80, h66, h67 = (hosts[p] for p in ("Ethernet0", "Ethernet80", "Ethernet66", "Ethernet67"))

    ports = ["Ethernet0", "Ethernet80", "Ethernet66", "Ethernet67"]
    before = _counters_snapshot(ssh, ports)

    results = {}
    errors  = {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(_run_iperf3_pair,
                      "Eth0↔Eth80(25G×QSFP)",
                      h0["test_ip"],  h0["mgmt_ip"],
                      h80["test_ip"], h80["mgmt_ip"],
                      host_ssh_creds, THRESH_25G): "Eth0↔Eth80",
            ex.submit(_run_iperf3_pair,
                      "Eth66↔Eth67(10G)",
                      h66["test_ip"], h66["mgmt_ip"],
                      h67["test_ip"], h67["mgmt_ip"],
                      host_ssh_creds, THRESH_10G): "Eth66↔Eth67",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                label, bps = fut.result()
                results[key] = bps
            except Exception as exc:
                errors[key] = str(exc)

    after = _counters_snapshot(ssh, ports)
    print("\n  Round 1 results:")
    for key, bps in results.items():
        print(f"    {key}: {bps/1e9:.2f} Gbps")
    _print_counter_delta(before, after)

    if errors:
        pytest.fail("Round 1 pair failures:\n" + "\n".join(f"  {k}: {v}" for k, v in errors.items()))


def test_throughput_round2(ssh, host_by_port, host_ssh_creds):
    """Round 2 — two pairs run simultaneously:
      Ethernet80 ↔ Ethernet81  (25G, same QSFP)  ≥ 20 Gbps
      Ethernet66 ↔ Ethernet0   (10G↔25G, cross-QSFP) ≥ 8 Gbps
    """
    hosts = _require_hosts(host_by_port, host_ssh_creds,
                           "Ethernet80", "Ethernet81", "Ethernet66", "Ethernet0")
    h80, h81, h66, h0 = (hosts[p] for p in ("Ethernet80", "Ethernet81", "Ethernet66", "Ethernet0"))

    ports = ["Ethernet80", "Ethernet81", "Ethernet66", "Ethernet0"]
    before = _counters_snapshot(ssh, ports)

    results = {}
    errors  = {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(_run_iperf3_pair,
                      "Eth80↔Eth81(25G same-QSFP)",
                      h80["test_ip"], h80["mgmt_ip"],
                      h81["test_ip"], h81["mgmt_ip"],
                      host_ssh_creds, THRESH_25G): "Eth80↔Eth81",
            ex.submit(_run_iperf3_pair,
                      "Eth66↔Eth0(10G×25G×QSFP)",
                      h66["test_ip"], h66["mgmt_ip"],
                      h0["test_ip"],  h0["mgmt_ip"],
                      host_ssh_creds, THRESH_10G): "Eth66↔Eth0",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                label, bps = fut.result()
                results[key] = bps
            except Exception as exc:
                errors[key] = str(exc)

    after = _counters_snapshot(ssh, ports)
    print("\n  Round 2 results:")
    for key, bps in results.items():
        print(f"    {key}: {bps/1e9:.2f} Gbps")
    _print_counter_delta(before, after)

    if errors:
        pytest.fail("Round 2 pair failures:\n" + "\n".join(f"  {k}: {v}" for k, v in errors.items()))


# ── 100G switch-to-switch tests ─────────────────────────────────────────────

def _eos_ssh():
    """Return a connected paramiko SSHClient to EOS."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(EOS_HOST, username=EOS_USER, password=EOS_PASSWD, timeout=10)
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

    sonic_ip = sonic_eth48_temp_ip   # 10.99.48.1
    eos_ip   = eos_eth_temp_ip_48    # 10.99.48.2

    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'pkill -f iperf3 2>/dev/null; "
                  f"nohup iperf3 -s -1 -B {eos_ip} -D 2>/dev/null &'")
        time.sleep(1)

        before = _counters_snapshot(ssh, ["Ethernet48"])
        out, err, rc = ssh.run(
            f"iperf3 -c {eos_ip} -B {sonic_ip} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15
        )
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        after = _counters_snapshot(ssh, ["Ethernet48"])
        assert bps >= THRESH_100G, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {THRESH_100G/1e9:.0f} Gbps"
        )
        print(f"\n  Ethernet48↔EOS throughput: {bps/1e9:.2f} Gbps")
        _print_counter_delta(before, after)
    finally:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null || true'")
        eos.close()


def test_throughput_100g_eth112(ssh, sonic_eth112_temp_ip, eos_eth_temp_ip_112):
    """Ethernet112 ↔ EOS Et16/1 at 100G; threshold ≥ 90 Gbps."""
    if not _iperf3_on_eos():
        pytest.skip("iperf3 not found in EOS bash — cannot run 100G switch-to-switch test")

    sonic_ip = sonic_eth112_temp_ip  # 10.99.112.1
    eos_ip   = eos_eth_temp_ip_112   # 10.99.112.2

    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'pkill -f iperf3 2>/dev/null; "
                  f"nohup iperf3 -s -1 -B {eos_ip} -D 2>/dev/null &'")
        time.sleep(1)

        before = _counters_snapshot(ssh, ["Ethernet112"])
        out, err, rc = ssh.run(
            f"iperf3 -c {eos_ip} -B {sonic_ip} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15
        )
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        after = _counters_snapshot(ssh, ["Ethernet112"])
        assert bps >= THRESH_100G, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {THRESH_100G/1e9:.0f} Gbps"
        )
        print(f"\n  Ethernet112↔EOS throughput: {bps/1e9:.2f} Gbps")
        _print_counter_delta(before, after)
    finally:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null || true'")
        eos.close()
