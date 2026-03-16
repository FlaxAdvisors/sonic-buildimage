# NF-08 — Port Channel (LAG/LACP): TEST PLAN

## Mapping

Test stage: `tests/stage_16_portchannel/test_portchannel.py` (18 tests)

## Required Hardware State

- rabbit-lorax (192.168.88.14, Arista EOS) powered on with Port-Channel1 configured
  (Et13/1 + Et14/1 in LACP active mode, IP 10.0.1.0/31)
- Ethernet16 and Ethernet32 admin-up on SONiC, RS-FEC configured
- teamd feature enabled, teamd container running
- PortChannel1 created with both members, IP 10.0.1.1/31
- swss restart loop fix applied

## Step-by-Step Test Actions

### TestTeamdFeature

#### test_teamd_feature_enabled

```bash
redis-cli -n 4 hget 'FEATURE|teamd' state
```

**Pass**: `enabled`
**Fail action**: `sudo config feature state teamd enabled`

#### test_teamd_container_running

```bash
docker ps --format '{{.Names}}' --filter name=teamd
```

**Pass**: output contains `teamd`

### TestPortChannelConfig

#### test_portchannel_exists_in_config_db

```bash
redis-cli -n 4 exists 'PORTCHANNEL|PortChannel1'
```

**Pass**: `1`

#### test_portchannel_admin_up

```bash
redis-cli -n 4 hget 'PORTCHANNEL|PortChannel1' admin_status
```

**Pass**: `up`

#### test_portchannel_members_in_config_db

For Ethernet16 and Ethernet32:
```bash
redis-cli -n 4 exists 'PORTCHANNEL_MEMBER|PortChannel1|Ethernet16'
```

**Pass**: `1` for both members

#### test_portchannel_ip_configured

```bash
redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|PortChannel1|*'
```

**Pass**: output contains `10.0.1.1/31`

### TestLACPState

#### test_portchannel_lacp_active_up

```bash
show interfaces portchannel
```

Parse line matching `PortChannel1`.

**Pass**: protocol field contains `(Up)` (e.g., `LACP(A)(Up)`)

#### test_both_members_selected

Parse member states from portchannel output.

**Pass**: both Ethernet16 and Ethernet32 show state `S` (Selected)

#### test_teamdctl_state_current

```bash
teamdctl PortChannel1 state
```

**Pass**: output contains `state: current` for both ports, and `active: yes`

### TestDBPropagation

#### test_lag_table_in_app_db

```bash
redis-cli -n 0 hget 'LAG_TABLE:PortChannel1' oper_status
```

**Pass**: `up`

#### test_lag_member_table_in_app_db

For each member:
```bash
redis-cli -n 0 hget 'LAG_MEMBER_TABLE:PortChannel1:Ethernet16' status
```

**Pass**: `enabled`

#### test_lag_in_state_db

```bash
redis-cli -n 6 hgetall 'LAG_TABLE|PortChannel1'
```

**Pass**: non-empty output

### TestASICDB

#### test_sai_lag_object_exists

```bash
redis-cli -n 2 hget COUNTERS_LAG_NAME_MAP PortChannel1   # get OID
redis-cli -n 1 exists "ASIC_STATE:SAI_OBJECT_TYPE_LAG:${OID}"
```

**Pass**: OID starts with `oid:`, existence returns `1`

#### test_sai_lag_member_objects_exist

```bash
redis-cli -n 1 keys 'ASIC_STATE:SAI_OBJECT_TYPE_LAG_MEMBER:*' | wc -l
```

**Pass**: >= 2

### TestLAGConnectivity

#### test_portchannel_ip_in_show

```bash
show ip interfaces
```

**Pass**: output contains `PortChannel1` and `10.0.1.1`

#### test_ping_peer_over_lag

```bash
ping -c5 -W2 10.0.1.0
```

**Pass**: rc=0, `0% packet loss`

### TestLAGFailover

#### test_failover_and_recovery

Full test sequence with timing:

1. `sudo config interface shutdown Ethernet16`
2. Wait 5s for LACP convergence
3. Check: PortChannel1 still shows `(Up)`, Ethernet32 state=`S`
4. Ping 10.0.1.0 with 3 packets → **Pass**: 0% loss
5. `sudo config interface startup Ethernet16`
6. Wait 8s for LACP reconvergence
7. Check: both Ethernet16 and Ethernet32 state=`S`
8. Ping 10.0.1.0 with 3 packets → **Pass**: 0% loss

### TestStandalonePortsUnaffected

#### test_standalone_ports_still_up

```bash
show interfaces status Ethernet48 Ethernet112
```

Parse oper and admin columns for each.

**Pass**: oper=`up` for both (LAG config must not affect non-member connected ports)

## Pass/Fail Criteria — Summary

| Test | Expected |
|---|---|
| teamd feature state | `enabled` |
| teamd container | running |
| PORTCHANNEL entry | present |
| admin_status | `up` |
| Both members in PORTCHANNEL_MEMBER | yes |
| IP 10.0.1.1/31 configured | yes |
| PortChannel1 LACP state | `(Up)` |
| Both members Selected | state=`S` |
| teamdctl state | `current`, `active: yes` |
| APP_DB LAG oper_status | `up` |
| LAG_MEMBER status | `enabled` |
| STATE_DB LAG entry | present |
| ASIC_DB LAG OID | present |
| ASIC_DB LAG_MEMBER count | >= 2 |
| Ping 10.0.1.0 | 0% loss |
| Failover: ping with 1 member | 0% loss |
| Recovery: both members Selected | yes |
| Standalone ports after failover | oper=up |

## State Changes and Restoration

| Change | Restoration |
|---|---|
| Shutdown Ethernet16 | `sudo config interface startup Ethernet16` + wait 8s |

The failover test restores Ethernet16 before exiting. If the test fails mid-way and
Ethernet16 remains shut, subsequent tests requiring both LAG members will fail.
Always run with the assumption that a failed test leaves Ethernet16 shut — run
`sudo config interface startup Ethernet16` manually before re-running if needed.

No `config save` is called in the test — the shutdown/startup is volatile.
