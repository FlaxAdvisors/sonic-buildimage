# NF-08 — Port Channel (LAG/LACP): PLAN

## Problem Statement

SONiC must form a LACP-based Link Aggregation Group (LAG) with the Arista EOS peer
(rabbit-lorax). This provides:
- Link redundancy (failover from one DAC cable to the other)
- L3 connectivity to the peer via 10.0.1.1/31 ↔ 10.0.1.0/31
- The foundation for BGP peering over a stable, redundant path

The topology is fixed:
- SONiC PortChannel1: Ethernet16 + Ethernet32 (Hare)
- Arista Port-Channel1: Et13/1 + Et14/1 (Rabbit)
- SONiC IP: 10.0.1.1/31
- Arista IP: 10.0.1.0/31

LACP requires teamd, which is disabled by default in SONiC. A swss restart loop caused
by teamd masking prevented stable operation initially.

## Proposed Approach

1. Enable teamd feature: `config feature state teamd enabled`
2. Create PortChannel1: `config portchannel add PortChannel1`
3. Remove any existing IP from member ports
4. Add members: `config portchannel member add PortChannel1 Ethernet16` (and Ethernet32)
5. Assign IP: `config interface ip add PortChannel1 10.0.1.1/31`
6. Save: `config save -y`
7. Fix swss restart loop (teamd masking issue in swss.sh)

## Files to Change

| File | Action |
|---|---|
| `/usr/local/bin/swss.sh` (on running switch) | Patch teamd masking check |

No repo files need changing for the core LAG configuration — it is entirely CLI-driven.
The swss.sh fix is a runtime patch; if it needs to be permanent, it should be tracked
in the platform postinst or a separate patch file.

## Acceptance Criteria

- `teamd` feature state = `enabled` in CONFIG_DB
- `teamd` container is running
- `show interfaces portchannel` shows `PortChannel1 LACP(A)(Up) Ethernet32(S) Ethernet16(S)`
- `teamdctl PortChannel1 state` shows both ports `state: current`, `active: yes`
- L3 ping to peer (10.0.1.0) succeeds with 0% packet loss
- LAG survives shutdown of one member (ping continues, other port stays Selected)
- Both members return to Selected after recovery

## Risks and Watch-Outs

- **swss restart loop**: The root cause is swss.sh checking if teamd exists in
  `systemctl list-units --all` (it does, even when masked) and adding it to
  `MULTI_INST_DEPENDENT`. teamd container is Exited → `docker-wait-any-rs` returns
  immediately → swss is killed → restart loop. Fix: check CONFIG_DB FEATURE|teamd state
  before adding to MULTI_INST_DEPENDENT.
- **EOS not SSH-reachable from build host when PortChannel1 is up**: Management VLAN/ACL
  restriction. Always use ProxyJump through SONiC: `ssh -J admin@192.168.88.12 admin@192.168.88.14`
- **IP address on member ports must be removed first**: `config portchannel member add` fails
  if the port has an IP configured. If the bgp container is dead, `config interface ip remove`
  fails — use redis-cli to delete keys directly.
- **Failover convergence time**: LACP with slow timers (fast_rate=false) takes ~3× the
  LACP timeout to reconverge. The test uses slow LACP (30s timeout). After member restore,
  wait 8 seconds before checking membership status.
- **PortChannel1 blocks Ethernet16/32 for other tests**: These ports cannot be used for
  standalone tests (speed change, FEC) while in the LAG without disrupting connectivity.
