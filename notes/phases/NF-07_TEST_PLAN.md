# NF-07 — Autoneg & FEC: TEST PLAN

## Mapping

Test stage: `tests/stage_15_autoneg_fec/test_autoneg_fec.py` (18 tests)

## Required Hardware State

- syncd and swss stable
- Connected ports Ethernet16/32/48/112 with RS-FEC already configured (from NF-04)
- Test port: Ethernet0 (disconnected, no active peer — safe to modify FEC/autoneg)

## Cleanup / Restore Helper

All mutating tests use `_cleanup_port(ssh, "Ethernet0")` in finally:
```python
redis-cli -n 4 hdel 'PORT|Ethernet0' autoneg fec adv_speeds adv_interface_types
```

This removes the test-added fields, leaving Ethernet0 in its baseline state.

## Step-by-Step Test Actions

### TestFecConnectedPorts

#### test_connected_ports_fec_rs_in_config_db

For each of {Ethernet16, Ethernet32, Ethernet48, Ethernet112}:
```bash
redis-cli -n 4 hget 'PORT|EthernetN' fec
```
**Pass**: `rs`

#### test_connected_ports_fec_rs_in_asic_db

Get OID from COUNTERS_PORT_NAME_MAP, then:
```bash
redis-cli -n 1 hget "ASIC_STATE:SAI_OBJECT_TYPE_PORT:${OID}" SAI_PORT_ATTR_FEC_MODE
```
**Pass**: `SAI_PORT_FEC_MODE_RS`

### TestFecConfig (all on Ethernet0)

#### test_fec_rs_accepted

```bash
sudo config interface fec Ethernet0 rs
redis-cli -n 4 hget 'PORT|Ethernet0' fec          # → rs
redis-cli -n 1 hget "ASIC_STATE:...:${OID}" SAI_PORT_ATTR_FEC_MODE  # → SAI_PORT_FEC_MODE_RS
```
**Pass**: both values correct. Cleanup runs in finally.

#### test_fec_none_accepted

Set rs first, then:
```bash
sudo config interface fec Ethernet0 none
redis-cli -n 4 hget 'PORT|Ethernet0' fec          # → none
redis-cli -n 1 hget "ASIC_STATE:...:${OID}" SAI_PORT_ATTR_FEC_MODE  # → SAI_PORT_FEC_MODE_NONE
```
**Pass**: both values correct.

#### test_fec_fc_rejected

```bash
sudo config interface fec Ethernet0 fc
```
**Pass**: rc != 0 OR combined output contains `not in` or `invalid`

### TestAutonegConfig (all on Ethernet0)

#### test_autoneg_enable_accepted

```bash
sudo config interface autoneg Ethernet0 enabled
```
**Pass**: rc=0

#### test_autoneg_enable_propagates_to_config_db

After enable:
```bash
redis-cli -n 4 hget 'PORT|Ethernet0' autoneg
```
**Pass**: `on`

#### test_autoneg_enable_propagates_to_app_db

```bash
redis-cli -n 0 hget 'PORT_TABLE:Ethernet0' autoneg
```
**Pass**: `on`

#### test_autoneg_programs_asic_db

```bash
redis-cli -n 1 hget "ASIC_STATE:...:${OID}" SAI_PORT_ATTR_AUTO_NEG_MODE
```
**Pass**: `true`
**Note**: SAI writes the attribute even though hardware AN is non-functional. This is
the current behavior as of 2026-03-13. Earlier versions of this test checked for `false`.

#### test_autoneg_disable_accepted

Enable then disable:
```bash
sudo config interface autoneg Ethernet0 disabled
redis-cli -n 4 hget 'PORT|Ethernet0' autoneg
```
**Pass**: rc=0, CONFIG_DB value = `off`

#### test_show_autoneg_status

```bash
show interfaces autoneg status Ethernet0
```
**Pass**: output contains `enabled`

### TestAdvertisedSpeeds

#### test_supported_speeds_in_state_db

```bash
redis-cli -n 6 hget 'PORT_TABLE|Ethernet0' supported_speeds
```
**Pass**: non-empty, contains `100000`; skip if not populated

#### test_advertised_speeds_accepted

After enabling autoneg:
```bash
sudo config interface advertised-speeds Ethernet0 40000,100000
redis-cli -n 4 hget 'PORT|Ethernet0' adv_speeds     # → 40000,100000
redis-cli -n 0 hget 'PORT_TABLE:Ethernet0' adv_speeds  # → 40000,100000
```
**Pass**: both DBs updated

#### test_advertised_speeds_shown_in_cli

```bash
show interfaces autoneg status Ethernet0
```
**Pass**: output contains `40G` or `40000`

#### test_advertised_types_accepted

```bash
sudo config interface advertised-types Ethernet0 CR4
redis-cli -n 4 hget 'PORT|Ethernet0' adv_interface_types
```
**Pass**: `CR4`

### TestDefaultState

#### test_default_autoneg_is_not_set

```bash
show interfaces autoneg status Ethernet4
```
**Pass**: output contains `N/A`

#### test_default_asic_autoneg_false

```bash
redis-cli -n 1 hget "ASIC_STATE:...:${OID_Ethernet4}" SAI_PORT_ATTR_AUTO_NEG_MODE
```
**Pass**: `false` (port Ethernet4 has never had autoneg configured)

#### test_connected_ports_autoneg_status

For each connected port:
```bash
redis-cli -n 4 hget 'PORT|EthernetN' autoneg
```
**Pass**: value is empty string, None, or `off`

## Pass/Fail Criteria — Summary

| Test | Expected |
|---|---|
| Connected ports CONFIG_DB fec | `rs` |
| Connected ports ASIC_DB FEC mode | `SAI_PORT_FEC_MODE_RS` |
| `fec rs` on Ethernet0 | ASIC_DB = SAI_PORT_FEC_MODE_RS |
| `fec none` on Ethernet0 | ASIC_DB = SAI_PORT_FEC_MODE_NONE |
| `fec fc` on Ethernet0 | Rejected (rc!=0 or "not in" in output) |
| autoneg enabled | rc=0, CONFIG_DB=on, APP_DB=on, ASIC_DB=true |
| autoneg disabled | rc=0, CONFIG_DB=off |
| adv_speeds | stored in CONFIG_DB and APP_DB |
| default autoneg unconfigured port | N/A in CLI, false in ASIC_DB |

## State Changes and Restoration

All mutating tests call `_cleanup_port()` in their finally block, removing `autoneg`,
`fec`, `adv_speeds`, `adv_interface_types` from CONFIG_DB for Ethernet0. No `config save`
is called — the cleanup is volatile. No effect on connected ports.
