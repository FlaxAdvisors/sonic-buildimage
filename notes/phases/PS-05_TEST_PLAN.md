# PS-05 TEST PLAN — System EEPROM

## Test Stage Mapping

`tests/stage_01_eeprom/`

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- `wedge100s-i2c-daemon` has run at least once (boot-time or manual trigger)
  Verify: `/run/wedge100s/syseeprom` exists and is non-empty
- pmon container running with `/run/wedge100s` bind-mounted

## Step-by-Step Test Actions

### 1. Verify daemon cache file

```python
import os

cache_path = '/run/wedge100s/syseeprom'
assert os.path.exists(cache_path), f"Daemon cache {cache_path} does not exist"
size = os.path.getsize(cache_path)
assert size >= 256, f"Cache file too small: {size} bytes"

with open(cache_path, 'rb') as f:
    data = f.read(8)
assert data == b'TlvInfo\x00', f"Bad ONIE magic: {data!r}"
```

### 2. Import and instantiate

```python
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.eeprom import SysEeprom

eeprom = SysEeprom()
```

No exception should be raised.

### 3. Verify `read_eeprom()` returns valid data

```python
raw = eeprom.read_eeprom()
assert raw is not None,       "read_eeprom() returned None"
assert isinstance(raw, (bytes, bytearray)), "read_eeprom() did not return bytes"
assert len(raw) >= 256,       f"raw data too short: {len(raw)} bytes"
assert raw[:8] == b'TlvInfo\x00', "ONIE magic mismatch in read_eeprom() result"
```

### 4. Verify `get_eeprom()` returns populated dict

```python
info = eeprom.get_eeprom()
assert isinstance(info, dict), f"get_eeprom() returned {type(info)}, expected dict"
assert len(info) > 0,          "get_eeprom() returned empty dict"
```

### 5. Verify required TLV fields by type code

```python
REQUIRED_CODES = {
    '0x21': 'Product Name',
    '0x22': 'Part Number',
    '0x23': 'Serial Number',
    '0x24': 'Base MAC Address',
}

for code, name in REQUIRED_CODES.items():
    assert code in info, f"TLV {code} ({name}) missing from EEPROM dict"
    val = info[code]
    assert val is not None and val != '', f"TLV {code} ({name}) is empty"
```

### 6. Verify known hardware values

```python
# These are the values from the physical unit in the lab.
# If testing on a different unit, update these or make them prefix checks.
serial = info.get('0x23', '')
mac    = info.get('0x24', '')
pn     = info.get('0x22', '')

assert 'AI0' in serial or len(serial) >= 8, f"Serial {serial!r} looks wrong"
assert ':' in mac,                          f"MAC {mac!r} not in XX:XX format"
assert mac != '00:00:00:00:00:00',          "MAC is all-zeros"

# Accept any non-empty part number
assert len(pn) >= 3, f"Part number {pn!r} too short"
```

### 7. Verify chassis integration

```python
from sonic_platform.chassis import Chassis
chassis = Chassis()
info2 = chassis.get_system_eeprom_info()
assert isinstance(info2, dict)
assert len(info2) > 0
assert '0x23' in info2   # Serial Number must be present
```

### 8. Verify `decode-syseeprom` CLI

Run on the switch (as separate SSH command, not inside pmon):

```bash
ssh admin@192.168.88.12 decode-syseeprom
```

Expected output includes lines like:
```
Product Name       : WEDGE100S
Serial Number      : AI09019591
Base MAC Address   : 00:90:fb:61:da:a1
```

If `decode-syseeprom` exits non-zero or produces no output, the platform
EEPROM subsystem is not correctly installed.

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| Daemon cache exists | File present, ≥ 256 bytes | Absent or too small |
| ONIE magic | `TlvInfo\x00` | Any other bytes |
| `get_eeprom()` type | `dict` | Not a dict |
| `get_eeprom()` not empty | `len > 0` | Empty dict |
| Product Name (0x21) | Non-empty string | Missing or empty |
| Serial Number (0x23) | Non-empty, ≥ 8 chars | Missing or empty |
| MAC (0x24) | Contains `:`, non-zero | Missing, empty, or all-zeros |
| Part Number (0x22) | Non-empty | Missing or empty |

## State Changes and Restoration

This test is read-only. The `_eeprom_cache` in the `SysEeprom` instance is
populated but this instance is discarded after the test. No files are written.
No hardware registers are touched.
