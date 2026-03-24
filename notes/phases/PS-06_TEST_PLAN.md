# PS-06 TEST PLAN — LED Control

## Test Stage Mapping

`tests/stage_08_led/` (or equivalent)

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- `wedge100s_cpld` kernel module loaded
- Device registered: `/sys/bus/i2c/devices/1-0032/led_sys1` exists and is writable
- Physical access to observe front panel LEDs (optional — tests verify sysfs values)

## Step-by-Step Test Actions

### 0. Record initial state for restoration

```python
cpld_path = '/sys/bus/i2c/devices/1-0032'

def cpld_read(attr):
    return int(open(f'{cpld_path}/{attr}').read().strip(), 0)

def cpld_write(attr, val):
    open(f'{cpld_path}/{attr}', 'w').write(str(val))

initial_sys1 = cpld_read('led_sys1')
initial_sys2 = cpld_read('led_sys2')
```

### 1. Verify chassis set_status_led() and get_status_led()

```python
import sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.chassis import Chassis
chassis = Chassis()

# Test green
result = chassis.set_status_led('green')
assert result is True, "set_status_led('green') returned False"
hw_val = cpld_read('led_sys1')
assert hw_val == 2, f"Expected led_sys1=2 after green, got {hw_val}"
api_color = chassis.get_status_led()
assert api_color == 'green', f"get_status_led() returned {api_color!r} after green"

# Test red
result = chassis.set_status_led('red')
assert result is True, "set_status_led('red') returned False"
hw_val = cpld_read('led_sys1')
assert hw_val == 1, f"Expected led_sys1=1 after red, got {hw_val}"
api_color = chassis.get_status_led()
assert api_color == 'red', f"get_status_led() returned {api_color!r} after red"

# Test off
result = chassis.set_status_led('off')
assert result is True, "set_status_led('off') returned False"
hw_val = cpld_read('led_sys1')
assert hw_val == 0, f"Expected led_sys1=0 after off, got {hw_val}"

# Test amber → maps to red
result = chassis.set_status_led('amber')
assert result is True
hw_val = cpld_read('led_sys1')
assert hw_val == 1, f"Expected led_sys1=1 for amber (→red), got {hw_val}"

# Test unknown color
result = chassis.set_status_led('purple')
assert result is False, "set_status_led('purple') should return False"
```

### 2. Verify unknown color does not change LED state

```python
chassis.set_status_led('green')
chassis.set_status_led('invalid')
assert cpld_read('led_sys1') == 2, "LED state changed on invalid color"
```

### 3. Verify LedControl plugin initialization

```python
from plugins.led_control import LedControl

ctrl = LedControl()
hw_sys1 = cpld_read('led_sys1')
assert hw_sys1 == 2, f"LedControl.__init__ did not set SYS1 to green (got {hw_sys1})"
```

### 4. Verify port_link_state_change() — up event

```python
# Simulate port going up — SYS2 should become green
ctrl._port_states = {}   # reset to known empty state
cpld_write('led_sys2', 0)  # start with SYS2 off

ctrl.port_link_state_change('Ethernet0', 'up')
hw_sys2 = cpld_read('led_sys2')
assert hw_sys2 == 2, f"Expected led_sys2=2 after link up, got {hw_sys2}"
```

### 5. Verify port_link_state_change() — down event (last port)

```python
# Bring Ethernet0 up (from step 4), then down
ctrl._port_states = {'Ethernet0': True}
ctrl.port_link_state_change('Ethernet0', 'down')
hw_sys2 = cpld_read('led_sys2')
assert hw_sys2 == 0, f"Expected led_sys2=0 after last link down, got {hw_sys2}"
```

### 6. Verify SYS1 not modified by port_link_state_change()

```python
chassis.set_status_led('green')
ctrl.port_link_state_change('Ethernet0', 'up')
ctrl.port_link_state_change('Ethernet0', 'down')
assert cpld_read('led_sys1') == 2, "port_link_state_change modified SYS1"
```

### 7. Restore initial state

```python
cpld_write('led_sys1', initial_sys1)
cpld_write('led_sys2', initial_sys2)
assert cpld_read('led_sys1') == initial_sys1
assert cpld_read('led_sys2') == initial_sys2
```

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| `set_status_led('green')` | Returns True, sysfs=2 | Returns False or sysfs≠2 |
| `set_status_led('red')` | Returns True, sysfs=1 | Returns False or sysfs≠1 |
| `set_status_led('amber')` | Returns True, sysfs=1 | Returns False or sysfs≠1 |
| `set_status_led('off')` | Returns True, sysfs=0 | Returns False or sysfs≠0 |
| `set_status_led('invalid')` | Returns False | Returns True |
| `get_status_led()` after green | `'green'` | Any other string |
| `get_status_led()` after red | `'red'` | Any other string |
| `LedControl.__init__` SYS1 | sysfs=2 (green) | sysfs≠2 |
| Link up → SYS2 | sysfs=2 (green) | sysfs≠2 |
| Last link down → SYS2 | sysfs=0 (off) | sysfs≠0 |
| SYS1 unaffected by link change | sysfs unchanged | sysfs modified |

## State Changes and Restoration

This test writes to `led_sys1` and `led_sys2`. The initial values are recorded
in step 0 and restored at the end of step 7. On the running system, ledd owns
SYS2 — after test restoration, ledd will update SYS2 on the next link state
change (no manual re-intervention needed).
