"""Unit tests for topology.json schema validation."""
import json, os, pytest

TOPOLOGY_PATH = os.path.join(os.path.dirname(__file__), "topology.json")

@pytest.fixture(scope="module")
def topology():
    with open(TOPOLOGY_PATH) as f:
        return json.load(f)

def test_required_keys(topology):
    for key in ("device", "breakout_ports", "vlans", "portchannels", "optical_ports", "hosts"):
        assert key in topology, f"Missing key: {key}"

def test_host_ports_in_vlan10(topology):
    vlan10 = next(v for v in topology["vlans"] if v["id"] == 10)
    vlan10_members = set(vlan10["members"])
    for h in topology["hosts"]:
        assert h["port"] in vlan10_members, (
            f"Host port {h['port']} not in VLAN 10 members: {vlan10_members}"
        )

def test_breakout_modes_valid(topology):
    valid_modes = {"1x100G[40G]", "4x25G[10G]", "4x10G"}
    for bp in topology["breakout_ports"]:
        assert bp["mode"] in valid_modes, f"Unknown mode: {bp['mode']}"

def test_portchannel_members_are_ports(topology):
    # PortChannel members should be un-broken-out ports
    breakout_parents = {bp["parent"] for bp in topology["breakout_ports"]}
    for pc in topology["portchannels"]:
        for member in pc["members"]:
            assert member not in breakout_parents, (
                f"PortChannel member {member} is a breakout parent"
            )

def test_optical_fec_values(topology):
    valid_fec = {"rs", "none", "fc"}
    for op in topology["optical_ports"]:
        assert op["fec"] in valid_fec, f"Unknown FEC: {op['fec']}"


import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.deploy import _validate_topology


def test_validate_topology_passes_with_valid_data(topology):
    """Valid topology.json passes validation without raising SystemExit."""
    _validate_topology(topology)  # should not raise


def test_validate_topology_fails_on_missing_host_port():
    bad = {
        "vlans": [{"id": 10, "members": ["Ethernet0"]}],
        "hosts": [{"port": "Ethernet99", "mgmt_ip": "1.2.3.4", "test_ip": "2.3.4.5"}],
    }
    with pytest.raises(SystemExit, match="Ethernet99"):
        _validate_topology(bad)
