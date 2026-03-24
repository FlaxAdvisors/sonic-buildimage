# NF-03 — Counters: TEST PLAN

## Mapping

Test stage: `tests/stage_12_counters/test_counters.py`

## Required Hardware State

- syncd container running and stable (not in restart loop)
- swss container running, counterd polling
- At least 4 ports with RS-FEC configured and oper=up (Ethernet16, 32, 48, 112)
  — needed for the RX_OK increment tests; other tests pass without active links

## Step-by-Step Test Actions

### 1. test_flex_counter_port_stat_enabled

```bash
counterpoll show
```

Find line matching `PORT_STAT` (not `BUFFER`).

**Pass**: line contains `enable` (case-insensitive) and optionally shows an interval <= 60000ms.

### 2. test_counters_port_name_map_all_ports

```bash
redis-cli -n 2 hgetall COUNTERS_PORT_NAME_MAP
```

Parse alternating field/value pairs; count entries where field contains `Ethernet`.

**Pass**: count >= 32

### 3. test_counters_db_oid_has_stat_entries

```bash
redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP Ethernet0     # get OID
redis-cli -n 2 hgetall "COUNTERS:${OID}" | head -20      # sample fields
```

**Pass**: output contains `SAI_PORT_STAT_`

### 4. test_counters_key_fields_present

Retrieve all field names from `COUNTERS:${OID}` for Ethernet0.

**Pass**: all of these are present:
- `SAI_PORT_STAT_IF_IN_OCTETS`
- `SAI_PORT_STAT_IF_IN_UCAST_PKTS`
- `SAI_PORT_STAT_IF_IN_ERRORS`
- `SAI_PORT_STAT_IF_IN_DISCARDS`
- `SAI_PORT_STAT_IF_OUT_OCTETS`
- `SAI_PORT_STAT_IF_OUT_UCAST_PKTS`
- `SAI_PORT_STAT_IF_OUT_ERRORS`
- `SAI_PORT_STAT_IF_OUT_DISCARDS`

### 5. test_show_interfaces_counters_exits_zero

```bash
show interfaces counters
```

**Pass**: rc=0, non-empty output

### 6. test_show_interfaces_counters_columns

**Pass**: header line contains all of:
`IFACE STATE RX_OK RX_BPS RX_UTIL RX_ERR RX_DRP TX_OK TX_BPS TX_UTIL TX_ERR TX_DRP`

### 7. test_show_interfaces_counters_port_rows

**Pass**: >= 32 rows matching `Ethernet\d+` pattern

### 8. test_counters_link_up_ports_show_U

For ports: Ethernet16, Ethernet32, Ethernet48, Ethernet112

**Pass**: STATE column = `U` for each port.
**Skip condition**: port not found in output (RS-FEC may not be configured).

### 9. test_counters_link_up_ports_have_rx_traffic

For the same 4 ports, parse RX_OK column (field index 2 after split).

**Pass**: RX_OK > 0 (LLDP traffic is always present on oper-up ports)

### 10. test_sonic_clear_counters

```bash
sonic-clear counters
```

Then immediately read `show interfaces counters` for Ethernet16.

**Pass**: `sonic-clear counters` returns rc=0; RX_OK < 100 immediately after clear
(only LLDP frames accumulate in the brief window between clear and read)

## Pass/Fail Criteria — Summary

| Test | Expected |
|---|---|
| PORT_STAT in counterpoll | enabled |
| COUNTERS_PORT_NAME_MAP Ethernet entries | >= 32 |
| SAI_PORT_STAT_* fields for Ethernet0 | all 8 required fields present |
| `show interfaces counters` exits | 0 |
| Counter column headers | all 12 expected columns |
| Port rows in counters output | >= 32 |
| STATE for link-up ports | U |
| RX_OK for link-up ports | > 0 |
| RX_OK after clear | < 100 |

## State Changes and Restoration

`sonic-clear counters` resets display offsets in COUNTERS_DB but does not modify
the underlying SAI counter values. Counters resume incrementing immediately after
clear. No configuration is changed; no teardown needed.
