"""Unit tests for gen-l3-config.py — no hardware required."""

import json
import subprocess
import sys
import os

# Make the ztp directory importable
ZTP_DIR = os.path.join(os.path.dirname(__file__), '..', '..',
                       'device', 'accton', 'x86_64-accton_wedge100s_32x-r0', 'ztp')
sys.path.insert(0, os.path.abspath(ZTP_DIR))

import gen_l3_config as glc

ARGS_A = dict(
    switch='a', hostname='wedge-a', mac='00:90:fb:61:da:a0',
    mgmt_ip='192.168.88.12/24', mgmt_gw='192.168.88.1',
    uplink_ip='203.0.113.1/30', uplink_gw='203.0.113.2',
)
ARGS_B = dict(
    switch='b', hostname='wedge-b', mac='00:90:fb:61:da:b0',
    mgmt_ip='192.168.88.22/24', mgmt_gw='192.168.88.1',
    uplink_ip='203.0.113.5/30', uplink_gw='203.0.113.6',
)


def test_wedge_a_asn():
    cfg = glc.generate_config(**ARGS_A)
    assert cfg['BGP_GLOBALS']['default']['local_asn'] == '65000'
    assert cfg['DEVICE_METADATA']['localhost']['bgp_asn'] == '65000'


def test_wedge_b_asn():
    cfg = glc.generate_config(**ARGS_B)
    assert cfg['BGP_GLOBALS']['default']['local_asn'] == '65001'
    assert cfg['DEVICE_METADATA']['localhost']['bgp_asn'] == '65001'


def test_wedge_a_router_id():
    cfg = glc.generate_config(**ARGS_A)
    assert cfg['BGP_GLOBALS']['default']['router_id'] == '10.1.0.1'


def test_wedge_b_router_id():
    cfg = glc.generate_config(**ARGS_B)
    assert cfg['BGP_GLOBALS']['default']['router_id'] == '10.1.0.2'


def test_node_neighbor_count():
    """48 nodes = 48 BGP_NEIGHBOR entries on the 10.0.x.x fabric."""
    cfg = glc.generate_config(**ARGS_A)
    node_neighbors = [k for k in cfg['BGP_NEIGHBOR'] if k.startswith('10.0.')]
    assert len(node_neighbors) == 48


def test_wedge_a_node1_interface():
    """Wedge A holds 10.0.1.0/31 for node 1 NIC0."""
    cfg = glc.generate_config(**ARGS_A)
    assert 'Ethernet16|10.0.1.0/31' in cfg['INTERFACE']
    assert 'Ethernet16' in cfg['INTERFACE']


def test_wedge_b_node1_interface():
    """Wedge B holds 10.0.1.2/31 for node 1 NIC1."""
    cfg = glc.generate_config(**ARGS_B)
    assert 'Ethernet16|10.0.1.2/31' in cfg['INTERFACE']


def test_wedge_a_node1_bgp_neighbor():
    """Node 1 NIC0 neighbor IP on Wedge A is 10.0.1.1, ASN 64512."""
    cfg = glc.generate_config(**ARGS_A)
    assert '10.0.1.1' in cfg['BGP_NEIGHBOR']
    n = cfg['BGP_NEIGHBOR']['10.0.1.1']
    assert n['asn'] == '64512'
    assert n['local_addr'] == '10.0.1.0'
    assert n['name'] == 'node01'


def test_wedge_b_node1_bgp_neighbor():
    """Node 1 NIC1 neighbor IP on Wedge B is 10.0.1.3, ASN 64512."""
    cfg = glc.generate_config(**ARGS_B)
    assert '10.0.1.3' in cfg['BGP_NEIGHBOR']
    n = cfg['BGP_NEIGHBOR']['10.0.1.3']
    assert n['asn'] == '64512'
    assert n['local_addr'] == '10.0.1.2'


def test_node48_asn():
    """Node 48 (last right-side port E111) should have ASN 64559."""
    cfg = glc.generate_config(**ARGS_A)
    # Node 48 NIC0 neighbor IP on Wedge A
    assert '10.0.48.1' in cfg['BGP_NEIGHBOR']
    assert cfg['BGP_NEIGHBOR']['10.0.48.1']['asn'] == '64559'


def test_interswitch_wedge_a():
    """Wedge A owns 10.255.0.0/31, peers with 10.255.0.1 (Wedge B, AS 65001)."""
    cfg = glc.generate_config(**ARGS_A)
    assert 'Ethernet0|10.255.0.0/31' in cfg['INTERFACE']
    assert '10.255.0.1' in cfg['BGP_NEIGHBOR']
    assert cfg['BGP_NEIGHBOR']['10.255.0.1']['asn'] == '65001'
    assert cfg['BGP_NEIGHBOR']['10.255.0.1']['name'] == 'wedge-b'


def test_interswitch_wedge_b():
    """Wedge B owns 10.255.0.1/31, peers with 10.255.0.0 (Wedge A, AS 65000)."""
    cfg = glc.generate_config(**ARGS_B)
    assert 'Ethernet0|10.255.0.1/31' in cfg['INTERFACE']
    assert '10.255.0.0' in cfg['BGP_NEIGHBOR']
    assert cfg['BGP_NEIGHBOR']['10.255.0.0']['asn'] == '65000'
    assert cfg['BGP_NEIGHBOR']['10.255.0.0']['name'] == 'wedge-a'


def test_breakout_ports():
    """All 12 server-facing parent ports must be set to 4x25G breakout."""
    cfg = glc.generate_config(**ARGS_A)
    expected = [
        'Ethernet16', 'Ethernet20', 'Ethernet24', 'Ethernet28', 'Ethernet32', 'Ethernet36',
        'Ethernet88', 'Ethernet92', 'Ethernet96', 'Ethernet100', 'Ethernet104', 'Ethernet108',
    ]
    for eth in expected:
        assert cfg['BREAKOUT_CFG'][eth]['brkout_mode'] == '4x25G[10G,1G]', \
            f'{eth} must be 4x25G[10G,1G]'


def test_non_server_ports_unchanged():
    """Peer, storage, reserved, and uplink ports must stay 1x100G."""
    cfg = glc.generate_config(**ARGS_A)
    unchanged = [
        'Ethernet0',   # P1 — peer switch
        'Ethernet40',  # P11 — storage
        'Ethernet44',  # P12 — storage
        'Ethernet48',  # P13 — reserved center
        'Ethernet112', # P29 — uplink
    ]
    for eth in unchanged:
        assert cfg['BREAKOUT_CFG'][eth]['brkout_mode'] == '1x100G[40G]', \
            f'{eth} must remain 1x100G[40G]'


def test_bgp_feature_enabled():
    cfg = glc.generate_config(**ARGS_A)
    assert cfg['FEATURE']['bgp']['state'] == 'enabled'
    assert cfg['FEATURE']['bgp']['auto_restart'] == 'enabled'


def test_mgmt_vrf_enabled():
    cfg = glc.generate_config(**ARGS_A)
    assert cfg['MGMT_VRF_CONFIG']['vrf_global']['mgmtVrfEnabled'] == 'true'


def test_mgmt_interface():
    cfg = glc.generate_config(**ARGS_A)
    assert 'eth0|192.168.88.12/24' in cfg['MGMT_INTERFACE']
    assert cfg['MGMT_INTERFACE']['eth0|192.168.88.12/24']['gwaddr'] == '192.168.88.1'


def test_static_default_route():
    cfg = glc.generate_config(**ARGS_A)
    assert '0.0.0.0/0' in cfg['STATIC_ROUTE']['default']
    assert cfg['STATIC_ROUTE']['default']['0.0.0.0/0']['nexthop'] == '203.0.113.2'
    assert cfg['STATIC_ROUTE']['default']['0.0.0.0/0']['ifname'] == 'Ethernet112'


def test_ecmp_enabled():
    cfg = glc.generate_config(**ARGS_A)
    assert cfg['BGP_GLOBALS']['default']['load_balance_mp_relax'] == 'true'


def test_bgp_timers():
    """All BGP neighbors must use 3s keepalive / 9s holdtime."""
    cfg = glc.generate_config(**ARGS_A)
    for ip, nbr in cfg['BGP_NEIGHBOR'].items():
        assert nbr['keepalive'] == '3', f'{ip}: keepalive must be 3'
        assert nbr['holdtime'] == '9', f'{ip}: holdtime must be 9'


def test_loopback_wedge_a():
    cfg = glc.generate_config(**ARGS_A)
    assert 'Loopback0|10.1.0.1/32' in cfg['LOOPBACK_INTERFACE']


def test_loopback_wedge_b():
    cfg = glc.generate_config(**ARGS_B)
    assert 'Loopback0|10.1.0.2/32' in cfg['LOOPBACK_INTERFACE']


def test_storage_interfaces():
    """Storage /30 subnets must appear in INTERFACE."""
    cfg = glc.generate_config(**ARGS_A)
    assert 'Ethernet40|10.2.1.1/30' in cfg['INTERFACE']
    assert 'Ethernet44|10.2.2.1/30' in cfg['INTERFACE']
    assert 'Ethernet80|10.2.3.1/30' in cfg['INTERFACE']
    assert 'Ethernet84|10.2.4.1/30' in cfg['INTERFACE']


def test_cli_produces_valid_json():
    """Running gen-l3-config.py as a script must emit valid JSON."""
    script = os.path.join(ZTP_DIR, 'gen-l3-config.py')
    result = subprocess.run(
        [sys.executable, script,
         '--switch', 'a', '--hostname', 'wedge-a',
         '--mac', '00:90:fb:61:da:a0',
         '--mgmt-ip', '192.168.88.12/24', '--mgmt-gw', '192.168.88.1',
         '--uplink-ip', '203.0.113.1/30', '--uplink-gw', '203.0.113.2'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f'Generator exited non-zero:\n{result.stderr}'
    cfg = json.loads(result.stdout)
    assert 'BGP_GLOBALS' in cfg
    assert 'INTERFACE' in cfg
    assert 'BGP_NEIGHBOR' in cfg
