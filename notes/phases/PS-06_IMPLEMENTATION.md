# PS-06 IMPLEMENTATION — LED Control

## Files Changed

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py`
  (LED methods within the Chassis class)
- `device/accton/x86_64-accton_wedge100s_32x-r0/plugins/led_control.py`
  (new file, `LedControl` plugin)

## Chassis LED Implementation

### Constants in `chassis.py`

```python
_CPLD_SYSFS = '/sys/bus/i2c/devices/1-0032'

_LED_ENCODE = {
    'green': 0x02,
    'red':   0x01,
    'amber': 0x01,   # hardware has no amber; map to red
    'off':   0x00,
}
_LED_DECODE = {0x02: 'green', 0x01: 'red', 0x00: 'off'}
```

### `set_status_led(color)`

1. Look up `color` in `_LED_ENCODE`; return `False` for unknown colors
2. Write the integer value (as a string) to `{_CPLD_SYSFS}/led_sys1`
3. Return `True` on success, `False` on exception

### `get_status_led()`

1. Read `{_CPLD_SYSFS}/led_sys1`, parse as `int(val, 0)`
2. Return `_LED_DECODE.get(val, STATUS_LED_COLOR_OFF)`
3. Return `STATUS_LED_COLOR_OFF` on any exception

Both methods control only **SYS1** (register 0x3e). SYS2 (0x3f) is owned
exclusively by `led_control.py`.

## LedControl Plugin Implementation

### CPLD sysfs write helper

```python
def _cpld_write(attr, val):
    with open(f'{_CPLD_SYSFS}/{attr}', 'w') as f:
        f.write(str(val))
```

Silently ignores exceptions (wrapped in `try/except Exception`).

### STATE_DB scan

`_state_db_port_states()` connects to STATE_DB via `swsscommon`, reads
`PORT_TABLE`, and returns `{port_name: bool(netdev_oper_status == 'up')}`.
Falls back to `{}` on any exception (swsscommon unavailable, DB not ready).

### `LedControl.__init__()`

1. Calls `_state_db_port_states()` to get current port states
2. Writes `led_sys1 = 0x02` (green) — SYS1 is green while SONiC is running
3. Computes `any_up = any(self._port_states.values())`
4. Writes `led_sys2 = 0x02` if any port is up, else `0x00`

### `port_link_state_change(port, state)`

1. Updates `self._port_states[port] = (state == 'up')`
2. Recomputes `any_up = any(self._port_states.values())`
3. Writes `led_sys2 = 0x02` if `any_up`, else `0x00`

Note: `led_sys1` is **not** touched by `port_link_state_change()`. SYS1 is
set once at init and then owned by `healthd` via `chassis.set_status_led()`.

## Register Encoding Summary

| Value | Meaning |
|---|---|
| 0x00 | Off |
| 0x01 | Red |
| 0x02 | Green |
| 0x04 | Blue |
| 0x08 | Off (blink) |
| 0x09 | Red blinking |
| 0x0a | Green blinking |
| 0x0c | Blue blinking |

Source: ONL `ledi.c`. The Python implementation only uses 0x00, 0x01, 0x02.
Blink modes and blue are not currently used.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64):
- CPLD at i2c-1/0x32 confirmed accessible via `wedge100s_cpld` driver
- `led_sys1` and `led_sys2` sysfs attributes confirmed writable
- Writing `2` to `led_sys1` → front panel SYS1 LED turns green (observed)
- Writing `1` to `led_sys1` → front panel SYS1 LED turns red (observed)
- Writing `0` to `led_sys2` → SYS2 off (observed)

## Remaining Known Gaps

- No per-port LED control. QSFP cage LEDs are driven by the BCM ASIC directly
  and are not accessible via the CPLD.
- Blink modes (0x08 modifier) are not exposed via the `set_status_led()` API.
- `LedControl` only tracks SYS2 state in memory; a `get_status_led()` on the
  chassis would read back from sysfs (accurate), but there is no
  `get_status_led()` on the `LedControl` plugin itself.
- Blue LED (0x04) is not used by any SONiC component currently.
