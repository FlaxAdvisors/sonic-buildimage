# NF-08 — Port Channel (LAG/LACP): IMPLEMENTATION

## What Was Built

No platform-specific files were created. LAG is handled entirely by teamd, orchagent, SAI.

### swss Restart Loop Fix (critical prerequisite)

**Root cause**: `swss.sh` adds `teamd` to `MULTI_INST_DEPENDENT` when:
1. `port_config.ini` is present (it is, on Wedge 100S)
2. `check_service_exists teamd` returns `true` (teamd unit exists in systemd even when masked)

When teamd was masked but existed in systemd, it appeared as "Exited" in `docker ps`.
`docker-wait-any-rs` returned immediately when given the Exited teamd container, causing
swss to be killed and restarted. This repeated every ~90s.

**Fix applied to `/usr/local/bin/swss.sh`**:
```bash
TEAMD_FEAT_STATE=$(sonic-db-cli CONFIG_DB hget "FEATURE|teamd" state 2>/dev/null)
if [[ $PORTS_PRESENT == 0 ]] && [[ $(check_service_exists teamd) == "true" ]] && \
   [[ "${TEAMD_FEAT_STATE}" == "enabled" ]]; then
    MULTI_INST_DEPENDENT="teamd"
fi
```

This gates the MULTI_INST_DEPENDENT addition on CONFIG_DB FEATURE state = `enabled`,
not just container existence. A masked teamd (state=disabled) no longer triggers the loop.

### PortChannel1 Configuration

Persisted to `config_db.json` via `config save`:

**CONFIG_DB entries:**
```
PORTCHANNEL|PortChannel1:
  admin_status=up, fast_rate=false, min_links=1, mtu=9100

PORTCHANNEL_MEMBER|PortChannel1|Ethernet16
PORTCHANNEL_MEMBER|PortChannel1|Ethernet32

PORTCHANNEL_INTERFACE|PortChannel1|10.0.1.1/31
```

### Arista EOS Configuration (rabbit-lorax)

```
interface Ethernet13/1
   no switchport
   channel-group 1 mode active
interface Ethernet14/1
   no switchport
   channel-group 1 mode active
interface Port-Channel1
   no switchport
   ip address 10.0.1.0/31
```

### LACP Negotiation State

**SONiC CLI** (verified on hardware 2026-03-02):
```
PortChannel1  LACP(A)(Up)  Ethernet32(S) Ethernet16(S)
```

**teamdctl output**:
```
state: current     (both ports — LACP PDUs actively exchanged)
active: yes
aggregator ID: 7   (negotiated with Arista)
```

**Arista EOS**:
```
Po1(U), Et13/1(PG+) Et14/1(PG+)
LACP partner: FFFF,00-90-fb-61-da-a0 (SONiC MAC)
Both ALGs+CD (active, aggregated, collecting/distributing)
```

### DB Pipeline State

| DB | Key | Verified value |
|---|---|---|
| CONFIG_DB | PORTCHANNEL\|PortChannel1 | admin_status=up |
| CONFIG_DB | PORTCHANNEL_MEMBER\|PortChannel1\|Ethernet16 | present |
| APP_DB | LAG_TABLE:PortChannel1 | oper_status=up |
| APP_DB | LAG_MEMBER_TABLE:PortChannel1:Ethernet16 | status=enabled |
| STATE_DB | LAG_TABLE\|PortChannel1 | oper_status=up, runner.active=true |
| ASIC_DB | SAI_OBJECT_TYPE_LAG | present (OID in COUNTERS_LAG_NAME_MAP) |
| ASIC_DB | SAI_OBJECT_TYPE_LAG_MEMBER | 2 entries (one per member) |

### Failover Results (verified on hardware 2026-03-02)

- Shut Ethernet16: LAG stays Up on Ethernet32 alone, ping 0% loss
- Ethernet16 shows (D)=deselected after shutdown
- Restore Ethernet16: both ports return to (S)=selected within 5–8s
- Ping 0% loss throughout failover/recovery cycle

### L3 Connectivity (verified on hardware 2026-03-02)

```
ping 10.0.1.0   (Rabbit PortChannel1 IP from SONiC)
  5 packets: 0% loss, avg 0.25ms

# From Rabbit:
ping 10.0.1.1   (SONiC PortChannel1 IP)
  5 packets: 0% loss, avg 0.15ms
```

## Hardware-Verified Facts

- verified on hardware 2026-03-02: LACP negotiated between SONiC and Arista EOS
- verified on hardware 2026-03-02: both Ethernet16 and Ethernet32 Selected
- verified on hardware 2026-03-02: L3 ping 0% loss over PortChannel1
- verified on hardware 2026-03-02: failover to single member: ping 0% loss
- verified on hardware 2026-03-02: recovery: both members Selected within 5-8s
- verified on hardware 2026-03-06: PortChannel1 intact after DPB on Ethernet80 (no interference)

## Remaining Known Gaps

- **BGP not configured**: The session over 10.0.1.1 has not been established. BGP container
  was exited for 8 months (unrelated to this port). L3 ping works but no routing protocol.
- **fast_rate=false**: Slow LACP timers (30s timeout). Fast timers untested — may improve
  failover time to <1s if enabled.
- **EOS ACL blocks direct management access**: 192.168.88.14 is not directly reachable from
  the build host when PortChannel1 is active. Always use ProxyJump through SONiC.
