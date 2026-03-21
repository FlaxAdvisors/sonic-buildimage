# Root Cause: LACP Breaks Dev Host → rabbit-lorax Reachability

(verified on hardware 2026-03-19)

## Symptom

When PortChannel1 (Ethernet16 + Ethernet32) is configured with LACP on the SONiC switch,
the development host (192.168.88.238) loses all IP connectivity to rabbit-lorax (192.168.88.14)
even though ARP still resolves correctly. SSH via a jump through hare-lorax continues to work.
The problem is completely reversed by removing the PortChannel1 members.

## Root Cause: MAC Address Poisoning

EOS and SONiC use different models for PortChannel1:

| Side    | PortChannel1 config          | Sends ARP? |
|---------|------------------------------|------------|
| SONiC   | L2 switchport, access VLAN 999 | No (no IP) |
| EOS (broken) | `no switchport`, IP 10.0.1.0/31 | **Yes** — GARPs for 10.0.1.0 with src MAC `00:90:fb:5f:d8:af` |

EOS uses a single system MAC (`00:90:fb:5f:d8:af`) for ALL interfaces — both
Management1 (192.168.88.14) and Po1 (10.0.1.0/31). When Po1 comes UP, EOS sends
gratuitous ARP frames for 10.0.1.0/31 out of Po1 with source MAC `00:90:fb:5f:d8:af`.

Those frames reach the management LAN switch (via some path from the data-plane DAC
cables back into the management segment) and the switch updates its MAC address table:

```
Before LACP: 00:90:fb:5f:d8:af → port A (rabbit-lorax Management1)
After LACP:  00:90:fb:5f:d8:af → port B (wherever the Po1 GARP arrived from)
```

From that point forward, any unicast frame destined to `00:90:fb:5f:d8:af` is
forwarded to the wrong port. The ICMP echo requests from the dev host physically leave
ens7f0, are correctly addressed (right MAC), traverse the management switch — and are
delivered to the wrong port. EOS Management1 never sees them.

### Why ARP still works after poisoning

ARP requests are **broadcast** (`ff:ff:ff:ff:ff:ff`). Broadcasts are forwarded to all
ports regardless of the MAC table, so the ARP request reaches rabbit-lorax Management1.
EOS replies with a unicast ARP reply (src `00:90:fb:5f:d8:af`) back to the dev host.
The switch, using its (now corrupted) table, forwards this unicast reply to port B — but
in that direction, the management switch re-learns the correct port from the source MAC
as it leaves rabbit-lorax's port. The ARP reply makes it back. This creates the confusing
state where ARP is REACHABLE but ICMP is 100% loss.

## Evidence

| Test | Result |
|------|--------|
| `ping 192.168.88.14` with LACP down | 0% loss, 0.16ms |
| `ping 192.168.88.14` with LACP up (before fix) | 100% loss |
| `tcpdump -i ens7f0` with LACP up | ICMP echo requests leave dev host correctly |
| `tcpdump -i ma1 icmp` on EOS with LACP up | 0 packets captured on Management1 |
| ARP for 192.168.88.14 with LACP up | REACHABLE (`00:90:fb:5f:d8:af`) — correct MAC |
| `tcpdump -i postprod` on bang-lorax | Only ARP broadcast visible; ICMP unicast absent (forwarded to specific wrong port, not flooded) |
| VLAN 999 isolation on SONiC alone | Does NOT fix the problem |

## Fix

Make EOS Po1 match SONiC's model — L2 switchport in VLAN 999, no IP:

```
interface Port-Channel1
   switchport access vlan 999
   ! (no ip address)
```

This prevents EOS from sending ARP frames from Po1 with the system MAC. The LACP PDUs
(destination `01:80:c2:00:00:02`, link-local multicast, never forwarded by switches) continue
to flow for LACP negotiation. No L3 traffic flows on PortChannel1; it exists solely for
stage_16 test purposes which add/remove IPs as needed.

Result after fix: `ping 192.168.88.14` with LACP up → 0% loss, 0.14ms. Direct SSH
from dev host to rabbit-lorax now works with LACP active.

## Persistent Baseline Config

On SONiC (hare-lorax):
- PortChannel1: Ethernet16 + Ethernet32, LACP active, min-links 1
- VLAN 999: PortChannel1 untagged access
- No IP on PortChannel1

On EOS (rabbit-lorax):
- Po1: `switchport access vlan 999` (no IP)
- VLAN 999 must exist on EOS side as well

EOS VLAN 999 creation (if not present):
```
vlan 999
   name lacp-isolation
```
