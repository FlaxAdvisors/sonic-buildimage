"""Stage 20 — Traffic Forwarding Verification.

Verifies the ASIC forwards packets over connected links and SAI counters
accurately reflect traffic. Runs on clean-boot baseline before stage_nn_posttest.

This stage owns its own PortChannel1 lifecycle via the stage20_setup
session fixture (create before tests, remove after). Do not depend on
PortChannel1 pre-existing from stage_16 — that stage's fixture already
removed it as part of its teardown.
"""

import os
import re
import sys
import time
import configparser
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LAG_PORTS = ["Ethernet16", "Ethernet32"]
PORTCHANNEL = "PortChannel1"
LAG_IP_HARE = "10.0.1.1/31"
STANDALONE_PORT = "Ethernet48"
STANDALONE_IP_HARE = "10.0.0.1/31"

def _load_peer_ip(cfg_key="peer_ip", fallback="10.0.1.0"):
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(os.path.dirname(__file__), "..", "target.cfg"))
    return cfg.get("links", cfg_key, fallback=fallback)

PEER_IP = _load_peer_ip("peer_ip", "10.0.1.0")
STANDALONE_PEER_IP = _load_peer_ip("standalone_peer_ip", "10.0.0.0")


@pytest.fixture(scope="session", autouse=True)
def stage20_setup(ssh):
    """Bring up PortChannel1 and standalone port for traffic testing."""

    for port in LAG_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    ssh.run(f"sudo config interface fec {STANDALONE_PORT} rs", timeout=15)

    # Wait for physical links to come up after FEC
    deadline = time.time() + 30
    while time.time() < deadline:
        out, _, rc = ssh.run("show interfaces status 2>&1", timeout=15)
        up_count = sum(1 for p in LAG_PORTS if any(p in l and " up " in l for l in out.splitlines()))
        if up_count >= len(LAG_PORTS):
            break
        time.sleep(5)

    # Remove any INTERFACE entries that would block portchannel member add
    for port in LAG_PORTS:
        ssh.run(f"redis-cli -n 4 del 'INTERFACE|{port}' > /dev/null 2>&1", timeout=10)

    ssh.run("sudo config feature state teamd enabled", timeout=15)
    time.sleep(3)

    ssh.run(f"sudo config portchannel add {PORTCHANNEL}", timeout=30)
    for port in LAG_PORTS:
        ssh.run(f"sudo config portchannel member add {PORTCHANNEL} {port}", timeout=30)
    ssh.run(f"sudo config interface ip add {PORTCHANNEL} {LAG_IP_HARE}", timeout=15)
    ssh.run(f"sudo config interface ip add {STANDALONE_PORT} {STANDALONE_IP_HARE}", timeout=15)
    time.sleep(45)  # LACP + ARP convergence

    yield

    ssh.run(f"sudo config interface ip remove {PORTCHANNEL} {LAG_IP_HARE}", timeout=15)
    ssh.run(f"sudo config interface ip remove {STANDALONE_PORT} {STANDALONE_IP_HARE}", timeout=15)
    for port in LAG_PORTS:
        ssh.run(f"sudo config portchannel member del {PORTCHANNEL} {port}", timeout=30)
    ssh.run(f"sudo config portchannel del {PORTCHANNEL}", timeout=30)
    # Restore INTERFACE entries for clean_boot.json compatibility
    for port in LAG_PORTS:
        ssh.run(f"redis-cli -n 4 hset 'INTERFACE|{port}' NULL NULL > /dev/null 2>&1", timeout=10)
    for port in LAG_PORTS + [STANDALONE_PORT]:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)


def _get_counter(ssh, port, stat):
    """Read a single counter value from COUNTERS_DB for the given port."""
    oid_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10
    )
    oid = oid_out.strip()
    val_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget 'COUNTERS:{oid}' {stat}", timeout=10
    )
    return int(val_out.strip() or "0")


def test_portchannel_rx_counters_increment(ssh):
    """5000-packet flood to peer increments PortChannel member RX_OK by >= 4500."""
    before = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_UCAST_PKTS") for p in LAG_PORTS]
    ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(2)
    after = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_UCAST_PKTS") for p in LAG_PORTS]
    delta = sum(after[i] - before[i] for i in range(len(LAG_PORTS)))
    assert delta >= 4500, (
        f"RX_OK delta across {LAG_PORTS} was {delta}, expected >= 4500.\n"
        f"Before: {before}, After: {after}"
    )


def test_portchannel_tx_counters_increment(ssh):
    """5000-packet flood generates TX_OK on LAG member ports."""
    before = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS") for p in LAG_PORTS]
    ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(2)
    after = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS") for p in LAG_PORTS]
    delta = sum(after[i] - before[i] for i in range(len(LAG_PORTS)))
    assert delta >= 4500, f"TX_OK delta={delta} < 4500: before={before} after={after}"


def test_standalone_port_rx_tx(ssh):
    """Ping flood via Ethernet48 increments both RX and TX counters."""
    rx_before = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_IN_UCAST_PKTS")
    tx_before = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS")
    ssh.run(f"sudo ping -f -c 1000 {STANDALONE_PEER_IP} -W 2 > /dev/null 2>&1", timeout=30)
    time.sleep(2)
    rx_after = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_IN_UCAST_PKTS")
    tx_after = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS")
    assert rx_after - rx_before >= 900, f"Ethernet48 RX delta too low: {rx_after - rx_before}"
    assert tx_after - tx_before >= 900, f"Ethernet48 TX delta too low: {tx_after - tx_before}"


def test_fec_error_rate_100g(ssh):
    """Correctable FEC error rate < 1e-6/s on connected ports under traffic load."""
    ports = LAG_PORTS + [STANDALONE_PORT]
    before = {p: _get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES") for p in ports}
    ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=30)
    time.sleep(1)
    after = {p: _get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES") for p in ports}
    elapsed = 6.0
    for p in ports:
        rate = (after[p] - before[p]) / elapsed
        assert rate < 1e-6, f"{p} FEC correctable rate {rate:.2e}/s exceeds 1e-6/s under load"


def test_counter_clear_accuracy(ssh):
    """After sonic-clear counters, connected port RX_OK <= 20 (LLDP only)."""
    ssh.run("sudo sonic-clear counters", timeout=15)
    time.sleep(2)
    for port in LAG_PORTS + [STANDALONE_PORT]:
        rx = _get_counter(ssh, port, "SAI_PORT_STAT_IF_IN_UCAST_PKTS")
        assert rx <= 20, (
            f"{port} has {rx} RX_OK after clear — expected <= 20 "
            f"(residual unicast only; LLDP is multicast and counted separately)"
        )
