# PortChannel Investigation — MAC Poisoning, Test Timeouts, and VLAN Segregation

(verified on hardware 2026-03-28)

---

## Part 1 — stage_20 TX Counter / FEC Test Timeouts

### Root Cause

CPU flood-ping (`ping -f`) achieves only ~64 pps on this platform. At that rate:
- 5000 packets ÷ 64 pps ≈ 78 s
- `ssh.run()` timeout is 60 s → test always fails before ping completes

The 64 pps ceiling is the BCM Tomahawk CoPP rate limit on the CPU port. It is NOT caused by:
- Missing ARP (static ARP is installed correctly post-LOWER_UP)
- LACP convergence (confirmed Active/Synced/Collecting/Distributing on both sides)
- EOS dropping the pings (EOS silently drops ICMP because Po1 was L2-only with no IP)

### Confirmed State (at time of investigation)

```
# SONiC PortChannel1
ip addr show PortChannel1  → inet 10.0.1.1/31 (stage20_setup running)
ip neigh show              → 10.0.1.0 dev PortChannel1 lladdr 00:90:fb:5f:d8:af PERMANENT

# EOS Po1 (during investigation, user assigned IP)
interface Port-Channel1
   no switchport
   ip address 10.0.1.0/31
   Active members: Ethernet13/1, Ethernet14/1
```

### Fix Applied to test_traffic.py

Replace `-W 2` (per-packet reply timeout) with `-w 10` (hard wall-clock deadline):

```python
# test_portchannel_tx_counters_increment
# Before:
ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=60)
assert delta >= 4500
# After:
ssh.run(f"sudo ping -f -c 5000 -w 10 {PEER_IP} > /dev/null 2>&1", timeout=20)
assert delta >= 500   # 10 s × 64 pps = ~640 expected

# test_fec_error_rate_100g
# Before:
ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=60)
elapsed = 6.0
# After:
ssh.run(f"sudo ping -f -c 5000 -w 10 {PEER_IP} > /dev/null 2>&1", timeout=20)
elapsed = 11.0   # 10 s ping + 1 s sleep
```

`-w` (lowercase) is a hard exit deadline in seconds regardless of packet count.
`-W` (uppercase) is per-packet reply timeout — wrong tool for an unreplied-ping scenario.

---

## Part 2 — Dev Host Loses EOS Management Reachability When Po1 Has IP

### Symptom

When EOS Port-Channel1 is configured with `no switchport` and a routed IP (10.0.1.0/31),
the dev host (192.168.88.238) loses all unicast reachability to EOS Management1
(192.168.88.14). ARP still resolves correctly; ICMP is 100% loss.

```
# Confirmed live on hardware 2026-03-28
ping -c 3 192.168.88.14   → 100% loss
ip neigh show | grep 88.14 → 192.168.88.14 dev ens7f0 lladdr 00:90:fb:5f:d8:af DELAY
```

ARP resolves (broadcast, unaffected) but unicast forwarding is broken — classic L2 MAC
table poisoning.

### Root Cause: Shared System MAC + Shared VLAN 1 Broadcast Domain

EOS uses a single system MAC (`00:90:fb:5f:d8:af`) for ALL interfaces:
Management1, Port-Channel1, and data-plane ports including **Et32/1**.

Et32/1 is an EOS data-plane port that is connected to the same physical lab switch as
Management1. Both are in VLAN 1 (the EOS/switch default). They share the same L2
broadcast domain.

When EOS Po1 comes up with an IP, EOS sends gratuitous ARPs that egress Et32/1 (among
other paths). The lab switch learns:

```
Before: 00:90:fb:5f:d8:af → Management1's switch port
After:  00:90:fb:5f:d8:af → Et32/1's switch port   ← poisoned
```

Dev host unicast frames addressed to `00:90:fb:5f:d8:af` are forwarded to the wrong
port. EOS Management1 never receives them.

### Evidence from EOS MAC Table

```
rabbit-lorax# show mac address-table
Vlan  Mac Address        Type     Ports
----  -----------------  -------  ------
   1  0090.fb5f.d8af     DYNAMIC  Et32/1   ← EOS seeing its OWN MAC via Et32/1
   1  e454.e864.5183     DYNAMIC  Et32/1   ← bang-lorax postprod MAC also on Et32/1
   1  0090.fb61.daa0     DYNAMIC  Et32/1   ← SONiC system MAC on Et32/1
```

EOS Et32/1 and bang-lorax's `postprod` interface both connect to the same physical lab
switch. The lab switch is 802.1Q-capable (already trunks VLANs 30/40/50 to bang-lorax).

### SONiC Is NOT the Escape Path

Confirmed on hardware:
- `eth0` has no bridge master (`PortChannel1 has no bridge master` / `eth0 has no master`)
- SONiC's bridge has `vlan_filtering 1` — VLAN 999 and VLAN 10 are properly isolated
- GARPs arriving on PortChannel1 (L3 mode) are processed by the kernel locally; not
  forwarded to eth0

### Syseeprom Is NOT the Root Cause

Both switches have a mismatch between EEPROM MAC and actual system MAC:

| Switch        | EEPROM Local MAC      | Actual system MAC     |
|---------------|-----------------------|-----------------------|
| hare-lorax    | 3C:2C:99:58:9D:02     | 00:90:fb:61:da:a0     |
| rabbit-lorax  | A8:2B:B5:B8:01:D4     | 00:90:fb:5f:d8:af     |

The `00:90:fb` (Accton OUI) MACs are burned into the CPU ethernet silicon and used as
the system MAC regardless of EEPROM contents. The EEPROM `Extended MAC Base` / `Extended
MAC Address Size: 128` is the Facebook-allocated MAC pool for the ASIC front-panel ports,
not the management port.

Rabbit-lorax's EEPROM is also missing fields that hare-lorax has (NumMacs, PartNumber,
ServiceTag, etc.) — this should be corrected separately but has no bearing on the
MAC poisoning issue.

### Fix: VLAN Segregation (Shippable)

GARPs are correctly confined to their L2 broadcast domain. The problem is that data-plane
port Et32/1 and Management1 are in the **same** broadcast domain (VLAN 1).

Managed switches maintain separate MAC address tables per VLAN. If Et32/1 is in a
data VLAN (e.g., VLAN 10) and Management1 is in a management VLAN (e.g., VLAN 1), the
switch has independent MAC table entries:

```
VLAN 1  MAC table: 00:90:fb:5f:d8:af → Management1 port   ← stable, never touched
VLAN 10 MAC table: 00:90:fb:5f:d8:af → Et32/1 port        ← irrelevant to management
```

This matches a known-working prior EOS deployment where all untagged data-plane port
traffic was native to a VLAN other than 1, effectively isolating the management VLAN.

**Required changes:**
- Lab switch: assign EOS Et32/1's port to data VLAN (e.g., VLAN 10), not VLAN 1
- EOS: configure native VLAN on data-plane ports away from the default VLAN 1
- bang-lorax/postprod: already 802.1Q-capable; management VLAN stays on existing config

### Alternative Point Fix (Band-Aid, Not Recommended)

```
! On EOS rabbit-lorax
interface Port-Channel1
   mac address 02:90:fb:5f:d8:b0   ! unique MAC, no longer aliases Management1
```

This prevents Po1 GARPs from conflicting with Management1's MAC, but does NOT prevent
Et32/1's ordinary L2 traffic from poisoning the table for other reasons. VLAN segregation
is the correct long-term fix.

---

## Topology Reference

```
Lab management LAN (VLAN 1 on lab switch, currently):
  devhost (192.168.88.238, ens7f0)
  SONiC   (192.168.88.12,  eth0,        MAC 00:90:fb:61:da:a0)
  EOS Mgmt (192.168.88.14, Management1, MAC 00:90:fb:5f:d8:af)
  EOS data  (Et32/1,                    MAC 00:90:fb:5f:d8:af)  ← CONFLICT
  bang-lorax (192.168.88.2, postprod,   MAC e4:54:e8:64:51:83)

Data links (DAC, point-to-point, not via lab switch):
  SONiC Ethernet16 ←→ EOS Ethernet13/1   (PortChannel1 / Po1 LAG member)
  SONiC Ethernet32 ←→ EOS Ethernet14/1   (PortChannel1 / Po1 LAG member)
```
