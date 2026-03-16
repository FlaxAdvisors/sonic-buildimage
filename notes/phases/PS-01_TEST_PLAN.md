# PS-01 TEST PLAN — Thermal Subsystem

## Test Stage Mapping

`tests/stage_04_thermal/`

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- `wedge100s-bmc-poller.timer` active and having fired at least once
  (verify: `/run/wedge100s/thermal_1` exists and is < 15 s old)
- pmon container running with `/run/wedge100s` bind-mounted read-only
- `coretemp` kernel module loaded (verify: `lsmod | grep coretemp`)

## Step-by-Step Test Actions

### 1. Import and instantiate

```python
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.thermal import Thermal, NUM_THERMALS
```

Assert `NUM_THERMALS == 8`.

Instantiate all sensors:
```python
thermals = [Thermal(i) for i in range(NUM_THERMALS)]
```

No exception should be raised.

### 2. Verify sensor names

```python
names = [t.get_name() for t in thermals]
assert names[0] == 'CPU Core'
assert names[1] == 'TMP75-1'
assert names[7] == 'TMP75-7'
assert len(set(names)) == 8   # all distinct
```

### 3. Verify temperature readings

```python
for i, t in enumerate(thermals):
    temp = t.get_temperature()
    assert temp is not None, f"Sensor {i} returned None"
    assert isinstance(temp, float), f"Sensor {i} returned non-float: {type(temp)}"
    assert 0.0 < temp < 85.0, f"Sensor {i} out of plausible range: {temp}"
```

Expected ranges under normal lab conditions (20–25 °C ambient):
- CPU Core: 35–70 °C
- TMP75-1 through TMP75-7: 15–45 °C

### 4. Verify thresholds

```python
for i, t in enumerate(thermals):
    high = t.get_high_threshold()
    crit = t.get_high_critical_threshold()
    assert high is not None and isinstance(high, float)
    assert crit is not None and isinstance(crit, float)
    assert crit > high, f"Sensor {i}: crit {crit} <= high {high}"
```

Exact expected values:
- CPU Core: high=95.0, crit=102.0
- TMP75-*:  high=70.0, crit=80.0

### 5. Verify presence and status

```python
for i, t in enumerate(thermals):
    assert t.get_presence() is True, f"Sensor {i} not present"
    assert t.get_status()   is True, f"Sensor {i} not OK"
```

### 6. Verify min/max recorded update

```python
t = Thermal(1)
# Before any read, min/max should be None
assert t.get_minimum_recorded() is None
assert t.get_maximum_recorded() is None
# After one read, both should be set
temp = t.get_temperature()
assert t.get_minimum_recorded() == temp
assert t.get_maximum_recorded() == temp
```

### 7. Verify chassis integration

```python
from sonic_platform.chassis import Chassis
chassis = Chassis()
thermals = chassis.get_all_thermals()
assert len(thermals) == 8
assert thermals[0].get_name() == 'CPU Core'
```

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| `NUM_THERMALS` | 8 | Any other value |
| All names distinct | 8 unique strings | Duplicates or wrong names |
| All temperatures non-None | All return float | Any returns None |
| CPU Core range | 35–70 °C | < 0 or > 105 |
| TMP75 range | 15–45 °C | < 0 or > 85 |
| All `high` thresholds | 70.0 (TMP75) / 95.0 (CPU) | None or wrong value |
| All `crit` thresholds | 80.0 (TMP75) / 102.0 (CPU) | None or wrong value |
| `get_presence()` | True for all 8 | False for any |

## State Changes and Restoration

This test is read-only. It does not write any files, load modules, or change
system configuration. No restoration is needed.

The only side effect is updating `_min_recorded` / `_max_recorded` on the
`Thermal` instances created during the test, which are discarded after the test.
