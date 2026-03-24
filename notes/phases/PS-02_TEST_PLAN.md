# PS-02 TEST PLAN — Fan Subsystem

## Test Stage Mapping

`tests/stage_05_fan/`

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- All 5 fan trays physically installed
- `wedge100s-bmc-poller.timer` active and having fired at least once
  (verify: `/run/wedge100s/fan_1_front` exists)
- pmon container running with `/run/wedge100s` bind-mounted read-only
- `/dev/ttyACM0` passed into pmon container (for `set_speed()` test)

## Step-by-Step Test Actions

### 1. Import and count

```python
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.fan import Fan, FanDrawer, NUM_FANS

assert NUM_FANS == 5
```

### 2. Instantiate FanDrawers and verify structure

```python
drawers = [FanDrawer(i) for i in range(1, NUM_FANS + 1)]
assert len(drawers) == 5

for i, d in enumerate(drawers, 1):
    fans = d.get_all_fans()
    assert len(fans) == 1, f"Drawer {i}: expected 1 fan, got {len(fans)}"
    assert fans[0].index == i, f"Drawer {i}: fan index mismatch"
```

### 3. Verify fan names

```python
for i, d in enumerate(drawers, 1):
    assert d.get_name() == f'FanTray {i}'
    fan = d.get_all_fans()[0]
    assert fan.get_name() == f'Chassis Fan - {i}'
```

### 4. Verify presence (all trays installed)

```python
for i, d in enumerate(drawers, 1):
    assert d.get_presence() is True,  f"Drawer {i} reports absent"
    assert d.get_all_fans()[0].get_presence() is True
```

### 5. Verify RPM readings

```python
for i, d in enumerate(drawers, 1):
    fan = d.get_all_fans()[0]
    rpm = fan.get_speed_rpm()
    assert rpm is not None,    f"Fan {i}: RPM is None (daemon not running?)"
    assert isinstance(rpm, int), f"Fan {i}: RPM type {type(rpm)}"
    assert 100 < rpm < 16000,  f"Fan {i}: RPM {rpm} out of plausible range"
```

Expected range under normal operation (bmc-poller at 100% duty): 7000–8500 RPM
(front), 4500–5500 RPM (rear). Since `get_speed_rpm()` returns `min(front,rear)`,
expect the rear rotor value.

### 6. Verify speed percentage

```python
for i, d in enumerate(drawers, 1):
    fan = d.get_all_fans()[0]
    pct = fan.get_speed()
    assert isinstance(pct, int)
    assert 0 <= pct <= 100, f"Fan {i}: speed% {pct} out of range"
```

### 7. Verify direction

```python
from sonic_platform_base.fan_base import FanBase
for d in drawers:
    fan = d.get_all_fans()[0]
    assert fan.get_direction() == FanBase.FAN_DIRECTION_INTAKE
```

### 8. Verify target speed raises NotImplementedError before set_speed

```python
import importlib
import sonic_platform.fan as fan_mod

# Reset module state (create fresh Fan instance that hasn't had set_speed called)
fan_mod._target_speed_pct = None
fan = Fan(1)
try:
    fan.get_target_speed()
    assert False, "Expected NotImplementedError"
except NotImplementedError:
    pass
```

### 9. Verify chassis integration

```python
from sonic_platform.chassis import Chassis
chassis = Chassis()
drawers = chassis.get_all_fan_drawers()
assert len(drawers) == 5
for d in drawers:
    assert len(d.get_all_fans()) == 1
```

### 10. Verify tolerance constant

```python
fan = Fan(1)
assert fan.get_speed_tolerance() == 20
```

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| `NUM_FANS` | 5 | Any other value |
| Drawers count | 5 | != 5 |
| Fans per drawer | 1 | != 1 |
| All presence True | All True | Any False (trays all installed) |
| All RPMs > 0 | 100–16000 | 0, None, or > 16000 |
| Direction | `FAN_DIRECTION_INTAKE` | Any other value |
| Target speed before set | `NotImplementedError` | Returns a value |

## State Changes and Restoration

This test is read-only except for step 8, which modifies the module-level
`_target_speed_pct` variable to `None`. After the test:

```python
import sonic_platform.fan as fan_mod
fan_mod._target_speed_pct = None   # already None; no restoration needed
```

No hardware state changes. `set_speed()` is not called in this test.
