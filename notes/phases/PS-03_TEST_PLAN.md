# PS-03 TEST PLAN — PSU Subsystem

## Test Stage Mapping

`tests/stage_06_psu/`

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- At least one PSU physically installed and powered (PSU2 in lab configuration)
- `wedge100s_cpld` kernel module loaded; `/sys/bus/i2c/devices/1-0032/` exists
- `wedge100s-bmc-poller.timer` active; `/run/wedge100s/psu_2_vin` exists
- pmon container running with `/run/wedge100s` bind-mounted

## Step-by-Step Test Actions

### 1. Import and count

```python
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.psu import Psu, NUM_PSUS

assert NUM_PSUS == 2
```

### 2. Instantiate

```python
psus = [Psu(i) for i in range(1, NUM_PSUS + 1)]
assert len(psus) == 2
assert psus[0].get_name() == 'PSU-1'
assert psus[1].get_name() == 'PSU-2'
```

### 3. Verify CPLD presence/pgood reads

```python
# For each PSU, determine ground truth from CPLD sysfs directly
import os

def cpld_read(attr):
    p = f'/sys/bus/i2c/devices/1-0032/{attr}'
    return int(open(p).read().strip(), 0)

for psu in psus:
    n = psu._index
    cpld_present = cpld_read(f'psu{n}_present') == 1
    cpld_pgood   = cpld_read(f'psu{n}_pgood')   == 1
    assert psu.get_presence()         == cpld_present, f"PSU{n} presence mismatch"
    assert psu.get_powergood_status() == cpld_pgood,   f"PSU{n} pgood mismatch"
```

### 4. Verify type and capacity

```python
for psu in psus:
    assert psu.get_type() == 'AC', f"{psu.get_name()} type is not AC"
    assert psu.get_capacity() == 650.0, f"{psu.get_name()} capacity != 650W"
```

### 5. Verify telemetry for powered PSU (PSU2 assumed powered)

```python
psu2 = psus[1]  # PSU-2
if psu2.get_presence() and psu2.get_powergood_status():
    vin  = psu2.get_input_voltage()
    iin  = psu2.get_input_current()
    vout = psu2.get_voltage()
    iout = psu2.get_current()
    pout = psu2.get_power()

    assert vin is not None,  "PSU2 VIN is None"
    assert iin is not None,  "PSU2 IIN is None"
    assert vout is not None, "PSU2 VOUT is None"
    assert iout is not None, "PSU2 IOUT is None"
    assert pout is not None, "PSU2 POUT is None"

    assert 100 < vin  < 265,  f"PSU2 VIN {vin} out of AC range"
    assert 0.0 < iin  < 10.0, f"PSU2 IIN {iin} out of range"
    assert  10 < vout < 15,   f"PSU2 VOUT {vout} out of DC range (expected ~12V)"
    assert 0.0 < iout < 60.0, f"PSU2 IOUT {iout} out of range"
    assert 0.0 < pout < 700,  f"PSU2 POUT {pout} out of range"
```

### 6. Verify LINEAR11 decoder directly

```python
from sonic_platform.psu import _pmbus_decode_linear11

# Known value: 0xF980 = mantissa -384, exponent -1 → -192.0
# (used as a sanity check on sign-extension logic)
# Positive example: 0x0C00 = mantissa 1536, exponent 0 → 1536.0 W? No:
# Example from datasheet: 0xD900 = exp=27-32=-5, man=0x100=256 → 256/32=8.0 A
raw = 0xD900
val = _pmbus_decode_linear11(raw)
assert abs(val - 8.0) < 0.01, f"LINEAR11 decode: expected 8.0, got {val}"
```

### 7. Verify `get_status()` is presence AND pgood

```python
for psu in psus:
    expected = psu.get_presence() and psu.get_powergood_status()
    assert psu.get_status() == expected, f"{psu.get_name()} status logic error"
```

### 8. Verify chassis integration

```python
from sonic_platform.chassis import Chassis
chassis = Chassis()
psus_from_chassis = chassis.get_all_psus()
assert len(psus_from_chassis) == 2
assert psus_from_chassis[0].get_name() == 'PSU-1'
assert psus_from_chassis[1].get_name() == 'PSU-2'
```

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| `NUM_PSUS` | 2 | Any other value |
| PSU names | 'PSU-1', 'PSU-2' | Wrong names |
| Presence matches CPLD | Agreement | Mismatch |
| pgood matches CPLD | Agreement | Mismatch |
| Type | `'AC'` | Any other string |
| Capacity | `650.0` | Any other float |
| VIN (PSU2 powered) | 100–265 V | None or out of range |
| VOUT (PSU2 powered) | 10–15 V | None or out of range |
| POUT (PSU2 powered) | 0–700 W | None or > 650 |
| `get_status()` | presence AND pgood | Inconsistent |

## State Changes and Restoration

This test is read-only. No files are written, no registers are written.
The `_psu_cache` is populated during the test but resets automatically on
next daemon poll. No restoration is needed.
