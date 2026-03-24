# PW-03 — BGP/L3: Test Plan

## Overview

Verify eBGP session establishment and route exchange between SONiC (hare-lorax,
`10.0.1.1/31`) and Arista EOS (rabbit-lorax, `10.0.1.0/31`) over PortChannel1.

## Required Hardware State

- SONiC running on Wedge 100S-32X (`192.168.88.12`)
- EOS running on peer Wedge 100S-32X (`192.168.88.14`)
- PortChannel1 operationally up on both sides (NF-08 complete)
- `10.0.1.1/31` assigned to PortChannel1 on SONiC
- `10.0.1.0/31` assigned to PortChannel1 on EOS
- FRR BGP container running on SONiC
- BGP configured on both sides per PW-03_PLAN

## Dependencies

- NF-08 (PortChannel1 LACP up) must pass before this test
- PW-03 must be implemented (BGP neighbor configured on both sides)

---

## Test Actions

### T1: IP connectivity over PortChannel1 (pre-BGP prerequisite)

```bash
ssh admin@192.168.88.12 ping -c 3 -W 2 10.0.1.0
```

**Pass:** 0% packet loss, RTT < 5 ms.
**Fail:** Any packet loss or unreachable — do not proceed with BGP tests.

### T2: BGP session state — SONiC side

```bash
ssh admin@192.168.88.12 show bgp summary
```

Look for:
```
Neighbor    V  AS  MsgRcvd  MsgSent  TblVer  InQ  OutQ  Up/Down  State/PfxRcd
10.0.1.0    4  XXXXX  ...                               Established  N
```

**Pass:** Neighbor `10.0.1.0` shows state `Established` (not `Active`, `Connect`, or `Idle`).

### T3: BGP session state — EOS side

```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show bgp summary | grep 10.0.1.1'
```

**Pass:** Neighbor `10.0.1.1` shows `Established` state.

### T4: Routes received from EOS on SONiC

```bash
ssh admin@192.168.88.12 show ip bgp neighbor 10.0.1.0 received-routes
```

**Pass:** At least one prefix listed (EOS PortChannel1 connected route `10.0.1.0/31`
or an EOS loopback).

### T5: Routes advertised from SONiC to EOS

```bash
ssh admin@192.168.88.12 show ip bgp neighbor 10.0.1.0 advertised-routes
```

**Pass:** SONiC's `10.0.1.0/31` connected route appears in the advertised list.

### T6: Route installed in SONiC kernel routing table

```bash
ssh admin@192.168.88.12 show ip route bgp
```

**Pass:** At least one `B>*` (BGP best) route appears, reachable via `10.0.1.0`
(PortChannel1 nexthop).

### T7: BGP keepalive timer — session stability

```bash
# Wait 90 seconds, then check session is still up
ssh admin@192.168.88.12 vtysh -c 'show bgp neighbor 10.0.1.0' | grep 'BGP state'
```

**Pass:** State remains `Established` after 90 seconds.

---

## Pass/Fail Criteria Summary

| Test | Pass condition |
|---|---|
| T1 | Ping `10.0.1.0` with 0% loss (prerequisite) |
| T2 | SONiC shows BGP neighbor `Established` |
| T3 | EOS shows BGP neighbor `Established` |
| T4 | At least one prefix received from EOS |
| T5 | SONiC's `/31` route advertised to EOS |
| T6 | BGP route in kernel table via PortChannel1 |
| T7 | Session remains Established after 90 s |

All tests must pass for PW-03 to be considered complete.
