# L3 Support Design — Wedge100S SONiC

**Date:** 2026-03-30
**Branch:** wedge100s
**Status:** Approved for implementation

---

## Overview

Complete Layer-3 routing support for the Accton Wedge100S-32X running SONiC, deployed as
a dual-switch Top-of-Rack (ToR) complex serving a 48-node Kubernetes cluster with Calico BGP.
Approach: eBGP-to-hosts (Calico) with static default upstream, structured to promote to full
eBGP upstream (case A) without redesign.

---

## In-Rack Physical Complex

```
                    ┌─── Upstream Router ───┐
                    │                       │
              Wedge A (uplinks)       Wedge B (uplinks)
              P29-32                  P29-32
                    │                       │
              P1-4 ◄─── inter-switch ──────► P1-4
             /     \                       /     \
     P5-10  P23-28  storage         P5-10  P23-28  storage
       │       │                      │       │
  (NIC0 of all 48 nodes)         (NIC1 of all 48 nodes)

    OOB AS4630 ── all 48 BMC ports + Wedge A eth0 + Wedge B eth0
```

Each node: NIC port 0 → Wedge A, NIC port 1 → Wedge B.
Storage/Yosemite v3 on P11/12/21/22: single-attach (no HA expectation), each device
connects to whichever switch it is physically closest to. The other switch learns the route
via the inter-switch BGP link.
OOB management is a separate AS4630 (or equivalent) carrying all BMC 1G/10G ports plus
both Wedge eth0 management ports — fully isolated from the data plane.

---

## Port Allocation (per Wedge)

| Ports | Ethernet | Role | Mode |
|---|---|---|---|
| 1–4   | E0, E4, E8, E12    | Peer switch (in-rack inter-switch BGP) | 100G or PortChannel |
| 5–10  | E16–E36            | Left/center rack nodes                 | **4×25G breakout** |
| 11–12 | E40, E44           | Storage / Yosemite v3 (single-attach)  | 100G routed |
| 13–20 | E48–E76            | Reserved (peer-rack, future fabric)    | 100G, no IP |
| 21–22 | E80, E84           | Storage / Yosemite v3 (single-attach)  | 100G routed |
| 23–28 | E88–E108           | Right/center rack nodes                | **4×25G breakout** |
| 29–32 | E112–E124          | Uplinks (2 active ECMP, 2 spare)       | 100G |

After 4×25G breakout on ports 5–10 and 23–28:
- Ports 5–10 yield sub-ports E16–E39 (24 server-facing 25G ports, left side)
- Ports 23–28 yield sub-ports E88–E111 (24 server-facing 25G ports, right side)
- Total: **48 server-facing 25G ports per switch = 48 dual-homed nodes**

---

## IP Addressing

### Fabric /31s — node links

```
Node n, NIC0 (→ Wedge A):  10.0.n.0/31    Wedge A = 10.0.n.0    Node = 10.0.n.1
Node n, NIC1 (→ Wedge B):  10.0.n.2/31    Wedge B = 10.0.n.2    Node = 10.0.n.3
  n = 1..48
  Left side  (nodes 1–24):   Wedge sub-ports E16–E39
  Right side (nodes 25–48):  Wedge sub-ports E88–E111
```

### Inter-switch link (P1–P4)

```
Wedge A: 10.255.0.0/31  (A = .0)
Wedge B: 10.255.0.1/31  (B = .1)
```

### Storage links

```
Storage A (E40):  10.2.1.0/30
Storage B (E44):  10.2.2.0/30
Storage C (E80):  10.2.3.0/30
Storage D (E84):  10.2.4.0/30
```

### Loopback (BGP router-id)

```
Wedge A: Loopback0  10.1.0.1/32
Wedge B: Loopback0  10.1.0.2/32
```

### Pod CIDRs (Calico default)

```
10.244.n.0/24 per node, n = 1..48  (fits within 10.244.0.0/16)
```

### Uplinks

```
Wedge A: E112, E116  REPLACE-WITH-PROVIDER-IP-A/PREFIX
Wedge B: E112, E116  REPLACE-WITH-PROVIDER-IP-B/PREFIX
Default: 0.0.0.0/0 → REPLACE-WITH-UPSTREAM-GW  (static, cases B/C)
```

---

## BGP Architecture

### ASN Scheme

```
Wedge A:          AS 65000
Wedge B:          AS 65001
Node n:           AS (64511 + n) →  node01=64512, node02=64513 … node48=64559
Upstream:         AS REPLACE-WITH-PROVIDER-ASN  (or 65100 if operator-controlled)
```

Private ASNs (64512–65534) — no registration required. Per-node unique ASNs avoid
BGP AS-path loop prevention problems that arise when both switches see the same node
ASN from different directions.

Pods, services, and containers do **not** have ASNs. Only devices that run a BGP daemon
need one. BGP is invisible to pods.

### Session Map

```
Each node:         eBGP → Wedge A  (over 10.0.n.0/31)
                   eBGP → Wedge B  (over 10.0.n.2/31)
                   advertises: 10.244.n.0/24

Single-attach:     eBGP → one switch only
                   Other switch learns route via inter-switch BGP automatically

Wedge A ↔ Wedge B: eBGP over 10.255.0.0/31
                   exchange all pod CIDRs for east-west in-rack routing
                   secondary uplink path if one switch's upstream drops

Each Wedge:        static default 0.0.0.0/0 → upstream GW  (cases B/C)
                   REPLACE with BGP neighbor for upstream when case A is available
```

### Prefix Policy

```
Nodes → Wedge:     accept 10.244.0.0/16 le 24 only (pod CIDRs)
Wedge → Nodes:     advertise 0.0.0.0/0 (default, enables pod internet egress)
Wedge A ↔ Wedge B: accept 10.244.0.0/16 le 24 (pod CIDRs for east-west)
Wedge → Upstream:  nothing (Calico natOutgoing handles egress masquerade at the node)
Upstream → Wedge:  default route only (static in B/C, BGP in A)
```

### BGP Timers

```
keepalive: 3s    holdtime: 9s
```

Default (60/180s) gives 3-minute failover — unacceptable for k8s workloads. 3/9s gives
~9-second failover. For sub-second failover in production, add BFD (SONiC + FRR + Calico
all support it) — this is a future enhancement, not required for initial L3 bring-up.

### ECMP

`load_balance_mp_relax: true` in BGP_GLOBALS. Both NIC ports of the same node are
equal-cost paths — traffic distributes across them. If one NIC or link drops, BGP
withdraws that path within one hold-timer interval.

---

## Container Enablement

| Container | Image | Change | Reason |
|---|---|---|---|
| `bgp` (docker-fpm-frr) | already present | **enable** | FRR runs all BGP sessions |
| `nat` | present | **leave disabled** | not needed — see NAT section |
| everything else | — | no change | |

One feature flip: `config feature state bgp enabled`.

### Why NAT is not on the switch

The Tomahawk ASIC has a limited hardware NAT table. SONiC's `nat` feature relies on SAI
NAT APIs whose support on Tomahawk is uncertain. More importantly, Calico's `natOutgoing`
flag is the correct architectural location for this function: each node masquerades its pod
traffic behind its own node IP before it leaves the NIC. NAT state is distributed across
48 nodes, no single point of failure. When upstream moves to case A (BGP), flip
`natOutgoing: false` and pods become fully routable end-to-end.

---

## config_db.json Changes

### BREAKOUT_CFG

```json
"BREAKOUT_CFG": {
    "Ethernet16": { "brkout_mode": "4x25G[10G]" },
    "Ethernet20": { "brkout_mode": "4x25G[10G]" },
    "Ethernet24": { "brkout_mode": "4x25G[10G]" },
    "Ethernet28": { "brkout_mode": "4x25G[10G]" },
    "Ethernet32": { "brkout_mode": "4x25G[10G]" },
    "Ethernet36": { "brkout_mode": "4x25G[10G]" },
    "Ethernet88": { "brkout_mode": "4x25G[10G]" },
    "Ethernet92": { "brkout_mode": "4x25G[10G]" },
    "Ethernet96": { "brkout_mode": "4x25G[10G]" },
    "Ethernet100": { "brkout_mode": "4x25G[10G]" },
    "Ethernet104": { "brkout_mode": "4x25G[10G]" },
    "Ethernet108": { "brkout_mode": "4x25G[10G]" }
}
```

Breakout must be applied and settled before INTERFACE entries on sub-ports are valid.
ZTP must trigger `config load_minigraph` or equivalent after config_db loads.

### DEVICE_METADATA

```json
"DEVICE_METADATA": {
    "localhost": {
        "hostname": "REPLACE-WITH-HOSTNAME",
        "platform": "x86_64-accton_wedge100s_32x-r0",
        "hwsku": "Accton-WEDGE100S-32X",
        "mac": "REPLACE-WITH-MAC",
        "type": "LeafRouter",
        "bgp_asn": "65000"
    }
}
```

### FEATURE (additions to existing)

```json
"FEATURE": {
    "bgp": {
        "state": "enabled",
        "auto_restart": "enabled",
        "has_per_asic_scope": "False",
        "has_global_scope": "True"
    }
}
```

### MGMT_INTERFACE and MGMT_VRF_CONFIG

```json
"MGMT_INTERFACE": {
    "eth0": {},
    "eth0|REPLACE-WITH-MGMT-IP/PREFIX": {
        "gwaddr": "REPLACE-WITH-MGMT-GW"
    }
},
"MGMT_VRF_CONFIG": {
    "vrf_global": {
        "mgmtVrfEnabled": "true"
    }
}
```

### LOOPBACK_INTERFACE

```json
"LOOPBACK_INTERFACE": {
    "Loopback0": {},
    "Loopback0|10.1.0.1/32": {}
}
```

### INTERFACE (abbreviated — full 96 entries generated by provisioning)

```json
"INTERFACE": {
    "Ethernet16": {},   "Ethernet16|10.0.1.0/31": {},
    "Ethernet17": {},   "Ethernet17|10.0.2.0/31": {},
    "...": "E16-E39 for nodes 1-24 (left side)",
    "Ethernet88": {},   "Ethernet88|10.0.25.0/31": {},
    "...": "E88-E111 for nodes 25-48 (right side)",
    "Ethernet40": {},   "Ethernet40|10.2.1.0/30": {},
    "Ethernet44": {},   "Ethernet44|10.2.2.0/30": {},
    "Ethernet80": {},   "Ethernet80|10.2.3.0/30": {},
    "Ethernet84": {},   "Ethernet84|10.2.4.0/30": {},
    "Ethernet0": {},    "Ethernet0|10.255.0.0/31": {},
    "Ethernet112": {},  "Ethernet112|REPLACE-WITH-UPLINK-IP/PREFIX": {}
}
```

### STATIC_ROUTE

```json
"STATIC_ROUTE": {
    "default": {
        "0.0.0.0/0": {
            "nexthop": "REPLACE-WITH-UPSTREAM-GW",
            "ifname": "Ethernet112"
        }
    }
}
```

### BGP_GLOBALS

```json
"BGP_GLOBALS": {
    "default": {
        "local_asn": "65000",
        "router_id": "10.1.0.1",
        "load_balance_mp_relax": "true",
        "graceful_restart_enable": "true",
        "graceful_restart_preserve_fw_state": "true"
    }
}
```

### BGP_PEER_GROUP

```json
"BGP_PEER_GROUP": {
    "NODES":    { "peer_group_name": "NODES" },
    "SWITCHES": { "peer_group_name": "SWITCHES" }
}
```

### BGP_NEIGHBOR (structure — 48 nodes + inter-switch + upstream placeholder)

```json
"BGP_NEIGHBOR": {
    "10.0.1.1":  { "asn": "64512", "peer_group_name": "NODES",    "local_addr": "10.0.1.0",   "name": "node01", "holdtime": "9", "keepalive": "3" },
    "10.0.2.1":  { "asn": "64513", "peer_group_name": "NODES",    "local_addr": "10.0.2.0",   "name": "node02", "holdtime": "9", "keepalive": "3" },
    "...":       "48 total node entries, generated by provisioning tool",
    "10.255.0.1": { "asn": "65001", "peer_group_name": "SWITCHES", "local_addr": "10.255.0.0", "name": "wedge-b", "holdtime": "9", "keepalive": "3" },
    "REPLACE-WITH-UPSTREAM-IP": { "asn": "REPLACE-WITH-UPSTREAM-ASN", "local_addr": "REPLACE-WITH-UPLINK-IP", "name": "upstream", "holdtime": "9", "keepalive": "3" }
}
```

### BGP_NEIGHBOR_AF

```json
"BGP_NEIGHBOR_AF": {
    "10.0.1.1|ipv4":    { "admin_status": "true", "soft_reconfiguration_in": "true", "prefix_list_in": "ACCEPT-POD-CIDR", "prefix_list_out": "SEND-DEFAULT" },
    "10.255.0.1|ipv4":  { "admin_status": "true", "soft_reconfiguration_in": "true", "prefix_list_in": "ACCEPT-POD-CIDR", "prefix_list_out": "ACCEPT-POD-CIDR" },
    "REPLACE-WITH-UPSTREAM-IP|ipv4": { "admin_status": "true", "prefix_list_in": "ACCEPT-DEFAULT", "prefix_list_out": "ACCEPT-NOTHING" }
}
```

---

## ZTP Template

### ztp-l3-sample.json (no change needed)

The existing pattern is correct — fetches per-switch config_db.json by hostname:

```json
{
    "ztp": {
        "01-configdb-json": {
            "dynamic-url": {
                "source": {
                    "prefix": "http://REPLACE-WITH-ZTP-SERVER/ztp/",
                    "identifier": "hostname",
                    "suffix": "_l3_config_db.json"
                }
            }
        },
        "02-breakout-apply": {
            "plugin": {
                "name": "sonic-cfggen",
                "args": "-d --write-to-db"
            }
        }
    }
}
```

The second plugin step ensures dynamic port breakout is applied and interfaces are
settled before BGP attempts to bind to sub-port addresses.

### Per-switch provisioning files (on ZTP server)

```
wedge-a_l3_config_db.json  —  ASN 65000, 10.1.0.1 router-id, 10.255.0.0 inter-switch
wedge-b_l3_config_db.json  —  ASN 65001, 10.1.0.2 router-id, 10.255.0.1 inter-switch
```

Both files share the same INTERFACE, BREAKOUT_CFG, and PORT structure. Per-switch
differences: DEVICE_METADATA.bgp_asn, BGP_GLOBALS.local_asn, BGP_GLOBALS.router_id,
LOOPBACK_INTERFACE IP, INTERFACE inter-switch IP, BGP_NEIGHBOR inter-switch peer address.

---

## Calico Side Requirements

These are Kubernetes resources, not SONiC config. Listed here for completeness.

```yaml
# IP Pool
apiVersion: projectcalico.org/v3
kind: IPPool
metadata:
  name: pod-cidr
spec:
  cidr: 10.244.0.0/16
  blockSize: 24          # one /24 per node
  natOutgoing: true      # masquerade pod egress at node NIC (cases B/C upstream)
  nodeSelector: all()

# Global BGP config
apiVersion: projectcalico.org/v3
kind: BGPConfiguration
metadata:
  name: default
spec:
  logSeverityScreen: Info
  nodeToNodeMeshEnabled: false   # we use explicit BGPPeer resources, not full mesh
  asNumber: 64512                # base — each node overrides with its own ASN

# BGPPeer toward Wedge A (applied to all nodes)
apiVersion: projectcalico.org/v3
kind: BGPPeer
metadata:
  name: wedge-a
spec:
  peerIP: 10.0.NODE_N.0    # Wedge A address on this node's /31 — templated per node
  asNumber: 65000

# BGPPeer toward Wedge B (applied to all nodes)
apiVersion: projectcalico.org/v3
kind: BGPPeer
metadata:
  name: wedge-b
spec:
  peerIP: 10.0.NODE_N.2    # Wedge B address on this node's /31 — templated per node
  asNumber: 65001
```

Per-node ASN (64512+n) is set in the Node resource `spec.bgp.asNumber` field, typically
by the k8s provisioning tool (Cluster API, kubeadm, etc.) at node join time.

---

## Case A Upstream Upgrade Path

When upstream router supports BGP — two changes only, no redesign:

1. Replace `STATIC_ROUTE 0.0.0.0/0` with populated BGP_NEIGHBOR for upstream peer
2. Set `natOutgoing: false` on the Calico IPPool

All node BGP sessions, inter-switch BGP, prefix filters, and IP addressing remain identical.

---

## Verification

```bash
# On each Wedge after bring-up:
show bgp summary              # 48 node sessions + 1 inter-switch + (1 upstream) = Established
show ip route bgp             # 48 × 10.244.n.0/24 learned
show ip route 0.0.0.0/0       # static default via upstream GW present
show interfaces status        # E16-E39 and E88-E111 sub-ports all up at 25G

# Cross-switch east-west reachability:
ping 10.0.48.1                # node 48 (right side, Wedge B ports) from Wedge A

# From a k8s node:
ip route show                 # default via 10.0.n.0 and 10.0.n.2 (both switches)
calicoctl node status         # both BGP peers Established
ping 10.244.x.1               # pod on a node homed to the other switch
```

---

## What Does NOT Change

- `pmon_daemon_control.json` — no change
- `system_health_monitoring_config.json` — no change
- `platform.json` — no change
- All platform daemons (xcvrd, psud, thermalctld, ledd) — no change
- L2 ZTP samples and l2-config_db.json — preserved as-is, parallel deployment option
- OOB management (eth0, MGMT_VRF) — isolated from data plane routing

---

## Open Items

- BFD for sub-second failover (future enhancement)
- IPv6 / dual-stack pod networking (future)
- VRFs for multi-tenant network isolation (future)
- MetalLB or kube-vip for LoadBalancer service type (Calico/k8s side, out of scope)
- Peer-rack interconnect topology (center ports P13–20, reserved)
- PortChannel vs routed port design for P1–4 peer switch link (deferred to peer-switch session)
