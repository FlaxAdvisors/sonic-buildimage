# PW-03 — BGP/L3: Plan

## Problem Statement

The Wedge 100S-32X SONiC instance has a Layer 3 PortChannel link to an Arista EOS peer:

| Parameter | SONiC (hare-lorax) | EOS peer (rabbit-lorax) |
|---|---|---|
| IP address | `10.0.1.1/31` | `10.0.1.0/31` |
| Interface | PortChannel1 | PortChannel1 |
| Members | Ethernet13/1 + Ethernet14/1 (EOS) | TBD — see NF-08 |
| Peer IP | `10.0.1.0` | `10.0.1.1` |

BGP peering has not been formally configured or tested. There is no `BGP_NEIGHBOR` entry
in the SONiC `config_db.json`. FRRouting (FRR) is the BGP daemon in SONiC.

## Proposed Approach

### Step 1: Verify PortChannel1 is up (prerequisite)

PortChannel1 must be operationally up before BGP can be configured. See NF-08 for
PortChannel bringup. Verify:
```bash
show interfaces PortChannel1
show interfaces PortChannel1 portchannel
```

### Step 2: Configure BGP via SONiC CLI

SONiC uses `config bgp` commands or direct FRR `vtysh` configuration. The simpler path
is `vtysh` since SONiC's BGP CLI layer may not support all required options:

```bash
sudo vtysh -c "conf t" \
  -c "router bgp 65100" \
  -c "bgp router-id 10.0.1.1" \
  -c "neighbor 10.0.1.0 remote-as 65200" \
  -c "neighbor 10.0.1.0 description EOS-rabbit-lorax" \
  -c "address-family ipv4 unicast" \
  -c "network 10.0.1.0/31" \
  -c "exit-address-family"
```

AS numbers are arbitrary (65100 for SONiC, 65200 for EOS); confirm the EOS AS number
before configuring. EOS peer must have a matching `neighbor 10.0.1.1 remote-as 65100`.

### Step 3: Make configuration persistent

SONiC BGP config via `vtysh` is lost on reboot. To persist:
- Use `sonic-cfggen` / `config save` workflow, OR
- Add to `/etc/frr/frr.conf` (backed by the BGP container image), OR
- Use `config bgp` CLI if it supports eBGP neighbor addition

The recommended SONiC-native approach is to add BGP configuration to `config_db.json`
via the `BGP_NEIGHBOR` and `DEVICE_METADATA` tables and let bgpcfgd translate to FRR config.

### Files to Change

No platform-specific files. BGP is a SONiC control-plane feature.

Configuration targets:
- `/etc/sonic/config_db.json` — add `BGP_NEIGHBOR`, `DEVICE_METADATA.bgp_asn`
- Alternatively, `/etc/frr/frr.conf` in the BGP container

### EOS Side Configuration

```
router bgp 65200
   neighbor 10.0.1.1 remote-as 65100
   neighbor 10.0.1.1 description SONiC-hare-lorax
   address-family ipv4
      neighbor 10.0.1.1 activate
```

(Adapt AS numbers after confirming EOS configuration.)

## Acceptance Criteria

- `show bgp summary` on SONiC shows neighbor `10.0.1.0` in state `Established`
- At least one prefix received from EOS peer (EOS loopback or connected route)
- At least one prefix advertised to EOS peer (SONiC connected route on PortChannel1)
- BGP session survives `pmon` restart (not required to survive SONiC reboot unless persisted)

## Risks

- **PortChannel1 must be up**: BGP over a down interface will not establish. This phase
  depends on NF-08 being complete and stable.
- **AS number coordination**: EOS AS number must be confirmed before configuring SONiC.
  Mismatched AS numbers cause `OPEN` message errors and session refusal.
- **SONiC BGP container restart**: BGP config via `vtysh` is in-memory only. If the BGP
  container restarts (e.g., pmon restart), the session drops and does not recover without
  persistent config.
- **Route leaking**: Advertising the wrong prefixes over BGP in a lab can affect reachability
  to the management network. Limit advertised networks to PortChannel1's /31 subnet.
