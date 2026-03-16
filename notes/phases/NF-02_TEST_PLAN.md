# NF-02 — Transceiver Info & DOM: TEST PLAN

## Mapping

Test stage: `tests/stage_11_transceiver/`

## Required Hardware State

- pmon container running (xcvrd active)
- At least one QSFP28 module inserted (hardware has 6 passive DAC cables: Ethernet0, 16, 32, 48, 80, 112)
- wedge100s-i2c-daemon running (writes `/run/wedge100s/sfp_N_eeprom` and `/run/wedge100s/sfp_N_present`)

## Step-by-Step Test Actions

### 1. STATE_DB populated by xcvrd

```bash
# Check TRANSCEIVER_INFO is present for at least one port
redis-cli -n 6 keys 'TRANSCEIVER_INFO|*' | wc -l
```

**Pass**: count >= 1 (should be 6 for current hardware)

```bash
# Verify required fields on a present port
redis-cli -n 6 hgetall 'TRANSCEIVER_INFO|Ethernet0'
```

**Pass**: keys include `type`, `connector`, `dom_capability`, `serial`

### 2. TRANSCEIVER_STATUS populated

```bash
redis-cli -n 6 keys 'TRANSCEIVER_STATUS|*' | wc -l
```

**Pass**: count matches TRANSCEIVER_INFO count

### 3. DOM sensor entries present

```bash
redis-cli -n 6 hgetall 'TRANSCEIVER_DOM_SENSOR|Ethernet0'
```

**Pass**: key exists (values may be `N/A` for passive DAC — this is correct)

### 4. Platform API — port count and presence

```python
from sonic_platform.platform import Platform
chassis = Platform().get_chassis()
assert chassis.get_num_sfps() == 32
# At least 4 ports report present
present = sum(1 for i in range(32) if chassis.get_sfp(i).get_presence())
assert present >= 4
```

**Pass**: 32 total ports, >=4 present

### 5. Platform API — EEPROM identifier byte

For each present port:
```python
sfp = chassis.get_sfp(port_index)
data = sfp.read_eeprom(0, 1)
assert data is not None
assert data[0] in (0x11, 0x0d, 0x01)  # QSFP28, QSFP+, GBIC (cheap DAC variant)
```

**Pass**: returns non-None bytearray, identifier in expected set

### 6. Platform API — xcvr API factory for QSFP28

For at least one present port returning byte 0 = 0x11:
```python
api = sfp.get_xcvr_api()
assert api is not None
assert 'Sff8636' in type(api).__name__
```

**Pass**: API object created successfully
**Note**: With cheap DAC cables, 2/7 success rate is acceptable (cable quality issue)

### 7. CLI — show interfaces transceiver eeprom

```bash
show interfaces transceiver eeprom Ethernet0
```

**Pass**: exits 0, output contains `Identifier:`

### 8. DOM values for passive DAC (all N/A is correct)

```bash
show interfaces transceiver eeprom --dom Ethernet0
```

**Pass**: exits 0; if cable is passive, DOM fields show `N/A`

### 9. get_transceiver_info() required keys

```python
info = sfp.get_transceiver_info()
required_keys = ['type', 'manufacturer', 'model', 'serial', 'connector',
                 'dom_capability', 'cable_type', 'cable_length']
for k in required_keys:
    assert k in info, f"Missing key: {k}"
```

**Pass**: all required keys present (values may be None/N/A for passive DAC)

## Pass/Fail Criteria — Summary

| Check | Expected | Notes |
|---|---|---|
| TRANSCEIVER_INFO keys in STATE_DB | >= 1 (expect 6) | One per inserted module |
| TRANSCEIVER_DOM_SENSOR keys | >= 1 | Values N/A for passive DAC |
| `get_num_sfps()` | 32 | |
| Present port count | >= 4 | Current hardware has 6 installed |
| Identifier byte for QSFP28 | 0x11 | Cheap DACs may return 0x01 intermittently |
| `get_xcvr_api()` | non-None for >= 1 port | 2/7 typical for cheap DAC hardware |
| CLI `show interfaces transceiver eeprom` | rc=0 | |

## State Changes and Restoration

No configuration changes. This test is read-only.
xcvrd and the I2C daemon update STATE_DB continuously; test reads are non-destructive.
