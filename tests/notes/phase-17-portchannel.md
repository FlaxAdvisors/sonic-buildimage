# Phase 17: Port Channel / LAG — Findings

*Verified on hardware 2026-03-02*

## Summary

LACP-based port channel (LAG) works end-to-end between SONiC (Hare) and Arista EOS (Rabbit).
Failover and recovery verified. No platform-specific code required — all LAG functionality
is handled by teamd, orchagent, and SAI.

## Configuration

### Hare (SONiC) — PortChannel1
```bash
# Enable teamd (was disabled by default)
sudo config feature state teamd enabled

# Create port channel
sudo config portchannel add PortChannel1

# Had to remove IP from Ethernet16 first (config interface ip remove
# failed due to dead bgp container; used redis-cli directly)
sudo redis-cli -n 4 del 'INTERFACE|Ethernet16|10.0.16.1/31'
sudo redis-cli -n 4 del 'INTERFACE|Ethernet16'

# Add members
sudo config portchannel member add PortChannel1 Ethernet16
sudo config portchannel member add PortChannel1 Ethernet32

# Add IP
sudo config interface ip add PortChannel1 10.0.1.1/31

# Save
sudo config save -y
```

### Rabbit (Arista EOS) — Port-Channel1
```
enable
configure
interface Port-Channel1
   no switchport
   ip address 10.0.1.0/31
interface Ethernet13/1
   no ip address
   channel-group 1 mode active
interface Ethernet14/1
   no switchport
   channel-group 1 mode active
end
write memory
```

## Verified Results

### LACP Negotiation (verified on hardware 2026-03-02)
- Hare: `show interfaces portchannel` → PortChannel1 LACP(A)(Up), Ethernet32(S) Ethernet16(S)
- Rabbit: `show port-channel dense` → Po1(U), Et13/1(PG+) Et14/1(PG+)
- teamdctl: both ports `state: current`, `aggregator ID: 65864`, `active: yes`
- Arista LACP internal: partner FFFF,00-90-fb-61-da-a0, both ports ALGs+CD (bundled)

### DB Pipeline (verified on hardware 2026-03-02)
- CONFIG_DB: PORTCHANNEL|PortChannel1 (admin_status=up, fast_rate=false, min_links=1, mtu=9100)
- CONFIG_DB: PORTCHANNEL_MEMBER|PortChannel1|Ethernet16, PORTCHANNEL_MEMBER|PortChannel1|Ethernet32
- APP_DB: LAG_TABLE:PortChannel1 (oper_status=up, admin_status=up)
- APP_DB: LAG_MEMBER_TABLE:PortChannel1:Ethernet{16,32} (status=enabled)
- STATE_DB: LAG_TABLE|PortChannel1 (oper_status=up, runner.active=true, team_device.ifinfo)
- ASIC_DB: SAI_OBJECT_TYPE_LAG + 2x SAI_OBJECT_TYPE_LAG_MEMBER objects
- COUNTERS_LAG_NAME_MAP: PortChannel1 → oid:0x20000000005e4

### L3 Connectivity (verified on hardware 2026-03-02)
- Hare → Rabbit: `ping 10.0.1.0` — 0% loss, avg 0.25ms
- Rabbit → Hare: `ping 10.0.1.1` — 0% loss, avg 0.15ms

### Failover (verified on hardware 2026-03-02)
- Shut Ethernet16: LAG stays Up on Ethernet32 alone, ping 0% loss
- Ethernet16 shows (D)=deselected, Ethernet32 stays (S)=selected
- Restore Ethernet16: both ports return to (S)=selected within 5-8s
- teamdctl shows `down count: 1` on Ethernet16 after recovery
- Ping 0% loss throughout entire failover/recovery cycle

## Issues Encountered

### bgp container dead — `config interface ip remove` fails
- `sudo config interface ip remove Ethernet16 10.0.16.1/31` fails with
  "Error response from daemon: Container ... is not running" (bgp container exited 8 months ago)
- Workaround: directly remove keys from CONFIG_DB via redis-cli

### teamd disabled by default
- Feature state was `disabled/enabled` — needed explicit `config feature state teamd enabled`
- After enabling, teamd container started immediately

### Rabbit not reachable from build host
- 192.168.88.14 unreachable from build host (timeout), but pingable from Hare
- Workaround: SSH ProxyJump through Hare (`ssh -J admin@192.168.88.12 admin@192.168.88.14`)

## Pytest

18/18 tests passing in `tests/stage_16_portchannel/test_portchannel.py`:
- TestTeamdFeature (2): feature enabled, container running
- TestPortChannelConfig (4): exists, admin up, members, IP
- TestLACPState (3): LACP active+up, both selected, teamdctl current
- TestDBPropagation (3): APP_DB, STATE_DB, LAG_MEMBER tables
- TestASICDB (2): SAI LAG + LAG_MEMBER objects
- TestLAGConnectivity (2): IP visible, ping peer
- TestLAGFailover (1): shutdown/verify/restore/verify cycle
- TestStandalonePortsUnaffected (1): Ethernet48, Ethernet112 still up
