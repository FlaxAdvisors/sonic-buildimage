#!/usr/bin/env python3
"""
Generate a complete L3 config_db.json for a Wedge100S-32X in the dual-ToR k8s design.

Port allocation (per switch):
  P1-4   (E0,4,8,12)    peer switch  — 1x100G
  P5-10  (E16-E36)      left nodes   — 4x25G breakout → sub-ports E16-E39
  P11-12 (E40,E44)      storage A/B  — 1x100G routed
  P13-20 (E48-E76)      reserved     — 1x100G, no IP
  P21-22 (E80,E84)      storage C/D  — 1x100G routed
  P23-28 (E88-E108)     right nodes  — 4x25G breakout → sub-ports E88-E111
  P29-32 (E112-E124)    uplinks      — 1x100G (only E112/P29 provisioned; E116/P30 deferred)

IP scheme:
  Node n NIC0 → Wedge A: 10.0.n.0/31  (A=.0, node=.1)
  Node n NIC1 → Wedge B: 10.0.n.2/31  (B=.2, node=.3)
  Inter-switch: 10.255.0.0/31  (A=.0, B=.1) on E0
  Storage: 10.2.{1-4}.0/30 on E40,E44,E80,E84
  Loopback: A=10.1.0.1/32  B=10.1.0.2/32

Usage:
  python3 gen-l3-config.py --switch a --hostname wedge-a \\
      --mac 00:90:fb:61:da:a0 \\
      --mgmt-ip 192.168.88.12/24 --mgmt-gw 192.168.88.1 \\
      --uplink-ip 203.0.113.1/30 --uplink-gw 203.0.113.2 \\
      > wedge-a_l3_config_db.json
"""

import argparse
import ipaddress
import json

# Parent port numbers (Ethernet<n>) that break out to 4×25G
_LEFT_PARENTS  = [16, 20, 24, 28, 32, 36]   # P5-P10
_RIGHT_PARENTS = [88, 92, 96, 100, 104, 108] # P23-P28

# Parent ports that remain 1×100G
_HUNDRED_G_PARENTS = [
    0, 4, 8, 12,                        # P1-4  peer switch
    40, 44,                             # P11-12 storage A/B
    48, 52, 56, 60, 64, 68, 72, 76,    # P13-20 reserved
    80, 84,                             # P21-22 storage C/D
    112, 116, 120, 124,                 # P29-32 uplinks
]

# Storage port → /30 subnet (switch gets first host .1)
_STORAGE_SUBNETS = {
    40: '10.2.1.0/30',
    44: '10.2.2.0/30',
    80: '10.2.3.0/30',
    84: '10.2.4.0/30',
}


def _subports(parents):
    """Expand parent port numbers to their 4×25G sub-port numbers."""
    result = []
    for p in parents:
        result.extend([p, p + 1, p + 2, p + 3])
    return result


def generate_config(switch, hostname, mac, mgmt_ip, mgmt_gw,
                    uplink_ip, uplink_gw, node_count=48):
    """Return a config_db dict for the given switch identity.

    Args:
        switch:     'a' or 'b'
        hostname:   switch hostname string
        mac:        chassis MAC (xx:xx:xx:xx:xx:xx)
        mgmt_ip:    eth0 IP with prefix length (e.g. '192.168.88.12/24')
        mgmt_gw:    eth0 default gateway IP
        uplink_ip:  uplink IP with prefix length (e.g. '203.0.113.1/30')
        uplink_gw:  upstream gateway IP (static default route target)
        node_count: number of k8s nodes (default 48, max 48 with this port layout)
    """
    is_a = switch.lower() == 'a'

    local_asn  = '65000' if is_a else '65001'
    peer_asn   = '65001' if is_a else '65000'
    router_id  = '10.1.0.1' if is_a else '10.1.0.2'
    loopback   = '10.1.0.1/32' if is_a else '10.1.0.2/32'

    # Inter-switch /31: A owns .0, B owns .1
    interswitch_local = '10.255.0.0/31' if is_a else '10.255.0.1/31'
    interswitch_peer  = '10.255.0.1'    if is_a else '10.255.0.0'
    peer_name         = 'wedge-b'       if is_a else 'wedge-a'

    # Fabric /31 offset: A holds .0/.2/.4... (even), B holds .2/.4... (even+2)
    # Per node n: A switch IP = 10.0.n.0, B switch IP = 10.0.n.2
    sw_octet4 = 0 if is_a else 2   # last octet of the switch address in the /31

    all_subports = _subports(_LEFT_PARENTS) + _subports(_RIGHT_PARENTS)

    cfg = {}

    # ── DEVICE_METADATA ──────────────────────────────────────────────────────
    cfg['DEVICE_METADATA'] = {
        'localhost': {
            'hostname': hostname,
            'platform': 'x86_64-accton_wedge100s_32x-r0',
            'hwsku': 'Accton-WEDGE100S-32X',
            'mac': mac,
            'type': 'LeafRouter',
            'bgp_asn': local_asn,
        }
    }

    # ── FEATURE ──────────────────────────────────────────────────────────────
    cfg['FEATURE'] = {
        'bgp': {
            'state': 'enabled',
            'auto_restart': 'enabled',
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
            'has_timer': 'False',
        }
    }

    # ── MGMT_INTERFACE / MGMT_VRF_CONFIG ─────────────────────────────────────
    cfg['MGMT_INTERFACE'] = {
        'eth0': {},
        f'eth0|{mgmt_ip}': {'gwaddr': mgmt_gw},
    }
    cfg['MGMT_VRF_CONFIG'] = {
        'vrf_global': {'mgmtVrfEnabled': 'true'}
    }

    # ── LOOPBACK_INTERFACE ───────────────────────────────────────────────────
    cfg['LOOPBACK_INTERFACE'] = {
        'Loopback0': {},
        f'Loopback0|{loopback}': {},
    }

    # ── BREAKOUT_CFG ─────────────────────────────────────────────────────────
    breakout = {}
    for p in _LEFT_PARENTS + _RIGHT_PARENTS:
        breakout[f'Ethernet{p}'] = {'brkout_mode': '4x25G[10G]'}
    for p in _HUNDRED_G_PARENTS:
        breakout[f'Ethernet{p}'] = {'brkout_mode': '1x100G[40G]'}
    cfg['BREAKOUT_CFG'] = breakout

    # ── INTERFACE ────────────────────────────────────────────────────────────
    iface = {}

    # Node fabric /31s
    for idx, subport in enumerate(all_subports):
        if idx >= node_count:
            break
        n = idx + 1                           # node number 1..48
        eth = f'Ethernet{subport}'
        sw_ip = f'10.0.{n}.{sw_octet4}'
        iface[eth] = {}
        iface[f'{eth}|{sw_ip}/31'] = {}

    # Storage /30s — switch gets first host address (.1)
    for port, subnet in _STORAGE_SUBNETS.items():
        eth = f'Ethernet{port}'
        net = ipaddress.IPv4Network(subnet)
        sw_ip = str(list(net.hosts())[0])
        iface[eth] = {}
        iface[f'{eth}|{sw_ip}/30'] = {}

    # Inter-switch /31
    iface['Ethernet0'] = {}
    iface[f'Ethernet0|{interswitch_local}'] = {}

    # Uplink (E112 = P29, primary uplink)
    iface['Ethernet112'] = {}
    iface[f'Ethernet112|{uplink_ip}'] = {}

    cfg['INTERFACE'] = iface

    # ── STATIC_ROUTE ─────────────────────────────────────────────────────────
    cfg['STATIC_ROUTE'] = {
        'default': {
            '0.0.0.0/0': {
                'nexthop': uplink_gw,
                'ifname': 'Ethernet112',
            }
        }
    }

    # ── BGP_GLOBALS ──────────────────────────────────────────────────────────
    cfg['BGP_GLOBALS'] = {
        'default': {
            'local_asn': local_asn,
            'router_id': router_id,
            'load_balance_mp_relax': 'true',
            'graceful_restart_enable': 'true',
            'graceful_restart_preserve_fw_state': 'true',
        }
    }

    # ── BGP_PEER_GROUP ───────────────────────────────────────────────────────
    cfg['BGP_PEER_GROUP'] = {
        'NODES':    {'peer_group_name': 'NODES'},
        'SWITCHES': {'peer_group_name': 'SWITCHES'},
    }

    # ── BGP_NEIGHBOR ─────────────────────────────────────────────────────────
    neighbors = {}

    for idx, subport in enumerate(all_subports):
        if idx >= node_count:
            break
        n = idx + 1
        node_asn  = str(64511 + n)                  # 64512..64559
        sw_ip     = f'10.0.{n}.{sw_octet4}'
        node_ip   = f'10.0.{n}.{sw_octet4 + 1}'    # switch+1 within /31
        neighbors[node_ip] = {
            'rrclient':        '0',
            'name':            f'node{n:02d}',
            'local_addr':      sw_ip,
            'nhopself':        '0',
            'holdtime':        '9',
            'asn':             node_asn,
            'keepalive':       '3',
            'peer_group_name': 'NODES',
        }

    # Inter-switch peer
    neighbors[interswitch_peer] = {
        'rrclient':        '0',
        'name':            peer_name,
        'local_addr':      interswitch_local.split('/')[0],
        'nhopself':        '0',
        'holdtime':        '9',
        'asn':             peer_asn,
        'keepalive':       '3',
        'peer_group_name': 'SWITCHES',
    }

    cfg['BGP_NEIGHBOR'] = neighbors

    # ── BGP_NEIGHBOR_AF ──────────────────────────────────────────────────────
    nbr_af = {}
    for ip in neighbors:
        nbr_af[f'{ip}|ipv4'] = {
            'admin_status': 'true',
            'soft_reconfiguration_in': 'true',
        }
    cfg['BGP_NEIGHBOR_AF'] = nbr_af

    return cfg


def main():
    p = argparse.ArgumentParser(
        description='Generate L3 config_db.json for Wedge100S dual-ToR k8s complex'
    )
    p.add_argument('--switch',    required=True, choices=['a', 'b'])
    p.add_argument('--hostname',  required=True)
    p.add_argument('--mac',       required=True, help='xx:xx:xx:xx:xx:xx')
    p.add_argument('--mgmt-ip',   required=True, help='e.g. 192.168.88.12/24')
    p.add_argument('--mgmt-gw',   required=True)
    p.add_argument('--uplink-ip', required=True, help='e.g. 203.0.113.1/30')
    p.add_argument('--uplink-gw', required=True)
    p.add_argument('--node-count', type=int, default=48)
    args = p.parse_args()

    cfg = generate_config(
        switch=args.switch,
        hostname=args.hostname,
        mac=args.mac,
        mgmt_ip=args.mgmt_ip,
        mgmt_gw=args.mgmt_gw,
        uplink_ip=args.uplink_ip,
        uplink_gw=args.uplink_gw,
        node_count=args.node_count,
    )
    print(json.dumps(cfg, indent=4))


if __name__ == '__main__':
    main()
