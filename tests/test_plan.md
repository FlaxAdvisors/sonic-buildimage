# Wedge 100S-32X Test Plan

## Physical Port Population (as of 2026-03-19)

| SONiC Port | Physical Port | Connected To | Cable Type | Notes |
|------------|---------------|--------------|------------|-------|
| Ethernet0  | Port 1        | Unknown device (no LLDP) | DAC 25G (4x25G breakout) | Et0 (Et1/1) oper UP; Et1–3 dark |
| Ethernet16 | Port 5        | rabbit-lorax Et13/1 | DAC 100G | PortChannel1 member; oper UP |
| Ethernet32 | Port 9        | rabbit-lorax Et14/1 | DAC 100G | PortChannel1 member; oper UP |
| Ethernet48 | Port 13       | rabbit-lorax Et15/1 | DAC 100G | Standalone; oper UP |
| Ethernet64 | Port 17       | Server ens1f1np1 (Mellanox) | QSFP+ 40GBASE-CR4, 2m (Mellanox, 2016) | 4x10G breakout; Et66/67 UP, Et64/65 dark (lanes 1–2 dead or unpatched on server) |
| Ethernet80 | Port 21       | localhost lan0 (MAC 1c:34:da:7f:9d:33) | QSFP28 25GBASE-CR4, 1m (2019) | 4x25G breakout; Et80/81 UP, Et82/83 dark |
| Ethernet104 | Port 27      | rabbit-lorax Et27/1 (Finisar CWDM4) | CWDM4 optical | Oper DOWN — SONiC TX not reaching EOS (physical fiber/TX issue, see phase-25 notes) |
| Ethernet108 | Port 28      | rabbit-lorax Et28/1 (ColorChip CWDM4) | CWDM4 optical | Oper DOWN — same physical TX issue |
| Ethernet112 | Port 29      | rabbit-lorax Et16/1 | DAC 100G | Standalone; oper UP |

## SONiC PortChannel Configuration (persistent baseline)

| Interface | Members | Mode | VLAN | Notes |
|-----------|---------|------|------|-------|
| PortChannel1 | Ethernet16, Ethernet32 | LACP active, min-links 1 | VLAN 999 untagged (isolation) | No IP on PortChannel1; EOS Po1 also `switchport access vlan 999` (no IP). Stage_16 tests add/remove IP as needed. |

**Why VLAN 999 isolation (both sides required)**: EOS and SONiC share the same system MAC (`00:90:fb:5f:d8:af`) across Management1 and Po1. When EOS Po1 was an L3 routed interface with IP 10.0.1.0/31, it sent gratuitous ARP frames with that MAC. Those frames leaked to the management LAN switch and poisoned its MAC table — the switch learned `00:90:fb:5f:d8:af` on the wrong port, causing all unicast to Management1 to be misdirected. ARP still worked (broadcast bypasses MAC table) but all IP unicast from dev host to 192.168.88.14 was silently dropped. VLAN 999 isolation on SONiC alone is insufficient; EOS Po1 must also be a switchport (no IP) to stop it from generating L3 ARP traffic. See `tests/notes/lacp-mgmt-reachability-root-cause.md`.

## Peer Node: rabbit-lorax (Arista EOS Wedge 100S)

- Access: direct SSH from dev host now works (192.168.88.14); jump via hare-lorax also works
- PortChannel1 (Po1): Et13/1 + Et14/1, LACP active, `switchport access vlan 999` (no IP); **UP** (LACP formed with hare-lorax)
- Et15/1: standalone, in VLAN 1, IP 10.0.0.0/31
- Et16/1: standalone, in VLAN 1 (no IP)
- Et27/1: optical CWDM4 ↔ hare-lorax Ethernet104 (Finisar, SN U4EA2RE) — link DOWN (physical TX issue)
- Et28/1: optical CWDM4 ↔ hare-lorax Ethernet108 (ColorChip) — link DOWN (physical TX issue)

## Per-Stage Requirements

| Stage | Requires | Configures | Unconfigures |
|-------|----------|------------|--------------|
| 01–10 | none | none | none |
| 11    | pmon running | none | none |
| 12    | syncd running | flex counter enable (if off) | restore |
| 13    | RS-FEC, link-up ports | RS-FEC on Et16/32/48/112 | remove FEC |
| 14    | breakout support | 4x25G on one port | restore 1x100G |
| 15    | FEC modes | RS-FEC then none on Et48 | restore FEC=none |
| 16    | LACP peer | IP on PortChannel1 (members pre-configured in baseline) | remove IP; restore to VLAN 999 untagged |
| 17    | restore done | none | none |
| 18    | pre-test ran | restore from snapshot | none |
| 19    | platform API | none | none |
| 20    | PortChannel1 up, traffic path | none (uses stage_16 state) | none |
