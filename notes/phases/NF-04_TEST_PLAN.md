# NF-04 — Link Status: TEST PLAN

## Mapping

Test stage: `tests/stage_13_link/test_link.py`

## Required Hardware State

- rabbit-lorax (Arista EOS, 192.168.88.14) powered on with Et13/1, Et14/1, Et15/1, Et16/1 admin-up
- 100G DAC cables installed: Ethernet16↔Et13/1, Ethernet32↔Et14/1, Ethernet48↔Et15/1, Ethernet112↔Et16/1
- SONiC RS-FEC configured on all 4 ports (persisted in config_db.json)
- syncd and swss stable (not in restart loop)

## Step-by-Step Test Actions

### 1. test_connected_ports_fec_rs_configured

For each port in {Ethernet16, Ethernet32, Ethernet48, Ethernet112}:
```bash
redis-cli -n 4 hget 'PORT|Ethernet16' fec
```

**Pass**: value = `rs`
**Fail message**: "Fix: sudo config interface fec <port> rs && sudo config save -y"

### 2. test_connected_ports_fec_rs_in_status

```bash
show interfaces status
```

Parse FEC column for each connected port.

**Pass**: FEC column = `rs` for all 4 connected ports

### 3. test_connected_ports_admin_up

For each connected port:
```bash
redis-cli -n 4 hget 'PORT|Ethernet16' admin_status
```

**Pass**: value = `up`
**Fail message**: "Fix: sudo config interface startup <port>"

### 4. test_connected_ports_oper_up

Parse oper and admin columns from `show interfaces status` for each connected port.

**Skip condition**: port is not admin-up
**Pass**: oper = `up` for all 4 connected ports
**Fail causes**: peer device down; pre-emphasis mismatch; syncd/orchagent restart cycle

### 5. test_port_state_in_app_db

For each connected port:
```bash
redis-cli -n 0 hgetall 'PORT_TABLE:Ethernet16'
```

**Pass**: output non-empty and contains `oper_status`

### 6. test_port_oper_status_state_db

For each connected port:
```bash
redis-cli -n 6 hget 'PORT_TABLE|Ethernet16' netdev_oper_status
```

**Pass**: value = `up`

### 7. test_asic_db_port_admin_state

For each connected port, get OID from COUNTERS_PORT_NAME_MAP, then:
```bash
redis-cli -n 1 hget "ASIC_STATE:SAI_OBJECT_TYPE_PORT:${OID}" SAI_PORT_ATTR_ADMIN_STATE
```

**Pass**: value = `true`
**Note**: SAI_PORT_ATTR_OPER_STATUS is NOT in ASIC_DB for this SAI version — do not test it

### 8. test_sys2_led_green_when_link_up

```bash
cat /sys/bus/i2c/devices/1-0032/led_sys2
```

If not `0x02` (green): restart ledd and re-check:
```bash
docker exec pmon supervisorctl restart ledd
sleep 3
cat /sys/bus/i2c/devices/1-0032/led_sys2
```

**Pass**: value = `0x02` (decimal 2)

### 9. test_lldp_neighbors_on_connected_ports

```bash
show lldp neighbors
```

**Pass**: each of {Ethernet16, Ethernet32, Ethernet48, Ethernet112} appears;
output contains `rabbit-lorax`

### 10. test_lldp_neighbor_port_mapping

Parse LLDP output by interface section.

**Pass**:
- Ethernet16 section contains `Ethernet13/1`
- Ethernet32 section contains `Ethernet14/1`
- Ethernet48 section contains `Ethernet15/1`
- Ethernet112 section contains `Ethernet16/1`

**Skip condition**: port not in LLDP output (link may be down)

## Pass/Fail Criteria — Summary

| Test | Expected |
|---|---|
| CONFIG_DB fec for connected ports | `rs` |
| show interfaces status fec column | `rs` |
| CONFIG_DB admin_status | `up` |
| show interfaces status oper | `up` |
| APP_DB PORT_TABLE oper_status key | present |
| STATE_DB netdev_oper_status | `up` |
| ASIC_DB SAI_PORT_ATTR_ADMIN_STATE | `true` |
| SYS2 LED | `0x02` (green) |
| LLDP neighbors | rabbit-lorax on all 4 ports |
| LLDP peer port IDs | Et13/1, Et14/1, Et15/1, Et16/1 |

## State Changes and Restoration

This test makes no configuration changes. All RS-FEC and admin-up settings are
pre-configured and persisted before the test runs. The ledd restart (if needed in
test 8) is non-destructive and does not affect port state.

## EOS Access Note

rabbit-lorax (192.168.88.14) is not directly SSH-reachable from the build host when
PortChannel1 LACP is active (management VLAN restriction). Always use jump via SONiC:
```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 'show interfaces status'
```
