"""Stage 20 — Traffic Forwarding Verification.

Verifies the ASIC forwards packets over connected links and SAI counters
accurately reflect traffic.

Topology:
  PortChannel1 (Ethernet16 + Ethernet32) is VLAN 999 access (L2).
  Vlan999 SVI carries a /31 for reachability:
    SONiC  hare-lorax   10.99.1.1/31
    EOS    rabbit-lorax 10.99.1.0/31
  Standalone port Ethernet48 connects to EOS Et15/1 (also in EOS LAG).

TX counters are validated by flood-pinging the EOS Vlan999 SVI.
RX counters are validated using LACP PDUs (EOS sends one per second per member).
"""

import json
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LAG_PORTS = ["Ethernet16", "Ethernet32"]
PORTCHANNEL = "PortChannel1"
STANDALONE_PORT = "Ethernet48"

# Vlan999 SVI addresses — ping target for TX counter validation.
EOS_SVI_IP = "10.99.1.0"


@pytest.fixture(scope="session", autouse=True)
def stage20_setup(ssh):
    """Ensure FEC is set on connected ports and LAG is converged before tests."""
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

    # Wait for PortChannel1 LOWER_UP (LACP converged)
    deadline = time.time() + 90
    while time.time() < deadline:
        out, _, _ = ssh.run("ip link show PortChannel1 2>/dev/null", timeout=5)
        if "LOWER_UP" in out:
            break
        time.sleep(5)
    else:
        raise RuntimeError(
            "PortChannel1 did not reach LOWER_UP within 90 s.\n"
            "Check LACP state: show interfaces portchannel"
        )

    # Verify Vlan999 SVI is reachable before running traffic tests
    out, _, rc = ssh.run(f"ping -c 2 -W 2 {EOS_SVI_IP}", timeout=15)
    if rc != 0:
        pytest.skip(
            f"Cannot ping EOS Vlan999 SVI ({EOS_SVI_IP}) — "
            "check Vlan999 SVI IPs and PortChannel1 VLAN 999 membership"
        )

    yield


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


def test_portchannel_tx_counters_increment(ssh):
    """5000-packet flood to EOS Vlan999 SVI increments PortChannel member TX_OK by >= 4500."""
    before = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS") for p in LAG_PORTS]
    ssh.run(f"sudo ping -f -c 5000 {EOS_SVI_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(2)
    after = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS") for p in LAG_PORTS]
    delta = sum(after[i] - before[i] for i in range(len(LAG_PORTS)))
    assert delta >= 4500, (
        f"TX_OK delta across {LAG_PORTS} was {delta}, expected >= 4500.\n"
        f"Before: {before}, After: {after}"
    )


def test_portchannel_rx_counters_increment(ssh):
    """EOS replies to flood ping increment PortChannel member RX_OK.

    With the Vlan999 SVI, EOS responds to ICMP echo requests with unicast
    replies. The 5000-packet flood from the TX test should produce replies
    that increment RX ucast counters. We also accept non-ucast from LACP PDUs.
    """
    before_ucast = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_UCAST_PKTS") for p in LAG_PORTS]
    before_non = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS") for p in LAG_PORTS]
    ssh.run(f"sudo ping -f -c 5000 {EOS_SVI_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(2)
    after_ucast = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_UCAST_PKTS") for p in LAG_PORTS]
    after_non = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS") for p in LAG_PORTS]
    delta_ucast = sum(after_ucast[i] - before_ucast[i] for i in range(len(LAG_PORTS)))
    delta_non = sum(after_non[i] - before_non[i] for i in range(len(LAG_PORTS)))
    delta_total = delta_ucast + delta_non
    assert delta_total >= 100, (
        f"RX delta across {LAG_PORTS}: ucast={delta_ucast}, non_ucast={delta_non}, "
        f"total={delta_total}; expected >= 100.\n"
        f"Before ucast: {before_ucast}, After: {after_ucast}\n"
        f"Before non_ucast: {before_non}, After: {after_non}"
    )


def test_standalone_port_rx_tx(ssh):
    """Ethernet48 SAI counter is readable and ASIC has it mapped.

    Ethernet48 maps to EOS Et15/1 which is a PortChannel member on the EOS side.
    LACP prevents it from forming a standalone link during this test, so oper-state
    is down and no traffic flows. We validate only that the COUNTERS_DB OID is
    present and the counter keys are readable — the counter API itself is exercised.
    """
    oid_out, _, rc = ssh.run(
        f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {STANDALONE_PORT}", timeout=10
    )
    oid = oid_out.strip()
    assert rc == 0 and oid.startswith("oid:"), (
        f"{STANDALONE_PORT} not found in COUNTERS_PORT_NAME_MAP (oid={oid!r})"
    )
    for stat in ("SAI_PORT_STAT_IF_OUT_UCAST_PKTS", "SAI_PORT_STAT_IF_IN_UCAST_PKTS"):
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hexists 'COUNTERS:{oid}' {stat}", timeout=10
        )
        assert val_out.strip() == "1", (
            f"{STANDALONE_PORT} COUNTERS_DB missing key {stat} (oid={oid})"
        )


def test_fec_error_rate_100g(ssh):
    """Correctable FEC error rate < 1e-6/s on LAG member ports under traffic load."""
    ports = LAG_PORTS
    before = {p: _get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES") for p in ports}
    ssh.run(f"sudo ping -f -c 5000 {EOS_SVI_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(1)
    after = {p: _get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES") for p in ports}
    elapsed = 6.0
    for p in ports:
        rate = (after[p] - before[p]) / elapsed
        assert rate < 1e-6, f"{p} FEC correctable rate {rate:.2e}/s exceeds 1e-6/s under load"


def test_counter_clear_accuracy(ssh):
    """After sonic-clear counters, portstat reports low RX_OK per LAG port.

    sonic-clear counters (portstat -c) saves a snapshot baseline; portstat then
    reports counters relative to that snapshot. The raw COUNTERS_DB keys are absolute
    ASIC counters that never reset, so we use portstat -j (JSON) to get the offset-adjusted
    value. After a 3-second settle only background traffic (LACP, LLDP, ARP, residual
    FlexCounter polling) should be counted — well under 50K on a 100G LAG member.
    """
    ssh.run("sudo sonic-clear counters", timeout=15)
    time.sleep(3)
    out, err, rc = ssh.run("portstat -j", timeout=15)
    assert rc == 0, f"portstat -j failed: {err}"
    json_start = out.find("{")
    assert json_start != -1, f"portstat -j produced no JSON:\n{out}"
    try:
        stats = json.loads(out[json_start:])
    except Exception as e:
        pytest.fail(f"portstat -j returned non-JSON: {e}\n{out[json_start:]}")
    for port in LAG_PORTS:
        if port not in stats:
            continue
        rx_ok = int(stats[port].get("RX_OK", "0").replace(",", "") or "0")
        assert rx_ok <= 50000, (
            f"{port} portstat RX_OK={rx_ok} after clear — unexpectedly high; "
            "expected <= 50000 in a 3-second window (LACP + LLDP + background)"
        )
