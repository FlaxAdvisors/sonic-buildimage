"""Stage 23 — Throughput verification via iperf3.

Tests:
  Round 1 — same-speed pairs (auto-scheduled into sub-rounds):
    test_throughput_round1:
      Ethernet24 ↔ Ethernet28  (100G↔100G)  ≥ 90 Gbps  -P 16 --zerocopy -w 4M
      Ethernet66 ↔ Ethernet67  ( 10G↔ 10G)  ≥  8 Gbps  -P 5
      Ethernet20 ↔ Ethernet22  ( 50G↔ 50G)  ≥ 40 Gbps  -P 6  --zerocopy
      Ethernet0  ↔ Ethernet80  ( 25G↔ 25G)  ≥ 20 Gbps  -P 5
    test_throughput_round1_reverse:
      Same pairs, server/client swapped — validates OUT counters.

  Round 2 — cross-speed pairs (auto-scheduled into sub-rounds):
    test_throughput_round2:
      Ethernet24 ↔ Ethernet20  (100G↔50G)   ≥ 40 Gbps  -P 8  --zerocopy
      Ethernet28 ↔ Ethernet66  (100G↔10G)   ≥  8 Gbps  -P 5
      Ethernet22 ↔ Ethernet80  ( 50G↔25G)   ≥ 20 Gbps  -P 5
    test_throughput_round2_reverse:
      Same pairs, server/client swapped.

  100G switch-to-switch:
    test_throughput_100g_eth48     Ethernet48  ↔ EOS Et15/1  ≥ 90 Gbps
    test_throughput_100g_eth112    Ethernet112 ↔ EOS Et16/1  ≥ 90 Gbps

Pairs are auto-scheduled into sub-rounds so no physical host runs more than
one iperf3 pair simultaneously.  Each pair gets a unique TCP port.

All tests skip (not fail) when: iperf3 absent, host SSH unreachable, EOS iperf3 absent.

Counter instrumentation: each test captures SAI COUNTERS_DB before/after iperf and
prints byte deltas, providing hardware-verified evidence that ASIC counters track iperf
traffic.  Counter reads are best-effort — failures are printed, not asserted.
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.iperf import (
    ROUND1_PAIRS, ROUND2_PAIRS, THRESH_100G,
    build_pairs, schedule_subrounds, run_iperf3_pair,
    _host_ssh, _run,
)

# iperf3 test duration seconds — 30s gives TCP time to reach steady-state throughput
IPERF_DURATION = 30

# EOS SSH coordinates — direct, no jump host needed when Po1 carries no IP.
EOS_HOST    = "192.168.88.14"
EOS_USER    = "admin"
EOS_PASSWD  = "0penSesame"

# Temporary /30 subnet for 100G switch-to-switch tests.
SONIC_TEMP_IP_ETH48   = "10.99.48.1/30"
EOS_TEMP_IP_ETH48     = "10.99.48.2"
SONIC_TEMP_IP_ETH112  = "10.99.112.1/30"
EOS_TEMP_IP_ETH112    = "10.99.112.2"


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    """Read SAI_PORT_STAT_IF_IN/OUT_OCTETS for named ports from COUNTERS_DB."""
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
    for port in sorted(set(before) & set(after)):
        din  = after[port][0] - before[port][0]
        dout = after[port][1] - before[port][1]
        print(f"    {port}: ASIC ΔRX={din/1e9:.3f} GB  ΔTX={dout/1e9:.3f} GB")


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


def _all_ports_from(pair_defs):
    ports = []
    for srv, cli, _, _, _, _ in pair_defs:
        ports.extend([srv, cli])
    return ports


# ── Round runner ───────────────────────────────────────────────────────────────

def _run_round(ssh, pairs, host_ssh_creds, round_name):
    """Execute iperf3 pairs with sub-round scheduling and counter instrumentation.

    Pairs are split into sub-rounds so no physical host is overloaded.
    Within a sub-round, pairs run in parallel.  Sub-rounds run sequentially.
    """
    all_ports = []
    for p in pairs:
        all_ports.extend([p["server_port"], p["client_port"]])
    before = _counters_snapshot(ssh, all_ports)

    results = {}
    errors  = {}
    subrounds = schedule_subrounds(pairs)
    port_counter = 5201

    for subround in subrounds:
        with ThreadPoolExecutor(max_workers=len(subround)) as ex:
            futures = {}
            for p in subround:
                srv = p["hosts"][p["server_port"]]
                cli = p["hosts"][p["client_port"]]
                fut = ex.submit(
                    run_iperf3_pair,
                    p["label"],
                    srv["test_ip"], srv["mgmt_ip"],
                    cli["test_ip"], cli["mgmt_ip"],
                    host_ssh_creds, p["threshold"],
                    duration=IPERF_DURATION,
                    parallel=p["parallel"],
                    port=port_counter,
                    zerocopy=p.get("zerocopy", False),
                    window=p.get("window"),
                )
                futures[fut] = p["label"]
                port_counter += 1
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    label, bps = fut.result()
                    results[key] = bps
                except Exception as exc:
                    errors[key] = str(exc)

    after = _counters_snapshot(ssh, all_ports)
    print(f"\n  {round_name} results ({len(subrounds)} sub-round(s)):")
    for key, bps in results.items():
        print(f"    {key}: {bps/1e9:.2f} Gbps")
    _print_counter_delta(before, after)

    if errors:
        pytest.fail(f"{round_name} pair failures:\n" +
                    "\n".join(f"  {k}: {v}" for k, v in errors.items()))


# ── Host-to-host round tests ──────────────────────────────────────────────────

def test_throughput_round1(ssh, host_by_port, host_ssh_creds):
    """Round 1 — same-speed pairs (auto-scheduled into sub-rounds)."""
    hosts = _require_hosts(host_by_port, host_ssh_creds, *_all_ports_from(ROUND1_PAIRS))
    _run_round(ssh, build_pairs(ROUND1_PAIRS, hosts), host_ssh_creds,
               "Round 1 (same-speed)")


def test_throughput_round1_reverse(ssh, host_by_port, host_ssh_creds):
    """Round 1 reverse — same pairs with server/client swapped."""
    hosts = _require_hosts(host_by_port, host_ssh_creds, *_all_ports_from(ROUND1_PAIRS))
    _run_round(ssh, build_pairs(ROUND1_PAIRS, hosts, reverse=True),
               host_ssh_creds, "Round 1 reverse (same-speed)")


def test_throughput_round2(ssh, host_by_port, host_ssh_creds):
    """Round 2 — cross-speed pairs (auto-scheduled into sub-rounds)."""
    hosts = _require_hosts(host_by_port, host_ssh_creds, *_all_ports_from(ROUND2_PAIRS))
    _run_round(ssh, build_pairs(ROUND2_PAIRS, hosts), host_ssh_creds,
               "Round 2 (cross-speed)")


def test_throughput_round2_reverse(ssh, host_by_port, host_ssh_creds):
    """Round 2 reverse — same pairs with server/client swapped."""
    hosts = _require_hosts(host_by_port, host_ssh_creds, *_all_ports_from(ROUND2_PAIRS))
    _run_round(ssh, build_pairs(ROUND2_PAIRS, hosts, reverse=True),
               host_ssh_creds, "Round 2 reverse (cross-speed)")


# ── 100G switch-to-switch tests ─────────────────────────────────────────────

def _eos_ssh():
    """Return a connected paramiko SSHClient to EOS, or skip if unreachable."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(EOS_HOST, username=EOS_USER, password=EOS_PASSWD, timeout=10)
    except (OSError, paramiko.SSHException) as e:
        pytest.skip(f"EOS peer ({EOS_HOST}) unreachable: {e}")
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
    ssh.run(f"sudo config interface ip add Ethernet48 {SONIC_TEMP_IP_ETH48}", timeout=10)
    yield SONIC_TEMP_IP_ETH48.split('/')[0]
    ssh.run(f"sudo config interface ip remove Ethernet48 {SONIC_TEMP_IP_ETH48}", timeout=10)


@pytest.fixture
def eos_eth_temp_ip_48():
    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'ip addr add {EOS_TEMP_IP_ETH48}/30 dev et15 2>/dev/null || true'")
        yield EOS_TEMP_IP_ETH48
    finally:
        _run(eos, f"bash -c 'ip addr del {EOS_TEMP_IP_ETH48}/30 dev et15 2>/dev/null || true'")
        eos.close()


@pytest.fixture
def sonic_eth112_temp_ip(ssh):
    ssh.run(f"sudo config interface ip add Ethernet112 {SONIC_TEMP_IP_ETH112}", timeout=10)
    yield SONIC_TEMP_IP_ETH112.split('/')[0]
    ssh.run(f"sudo config interface ip remove Ethernet112 {SONIC_TEMP_IP_ETH112}", timeout=10)


@pytest.fixture
def eos_eth_temp_ip_112():
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
        pytest.skip("iperf3 not found in EOS bash")

    sonic_ip = sonic_eth48_temp_ip
    eos_ip   = eos_eth_temp_ip_48

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
        pytest.skip("iperf3 not found in EOS bash")

    sonic_ip = sonic_eth112_temp_ip
    eos_ip   = eos_eth_temp_ip_112

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
