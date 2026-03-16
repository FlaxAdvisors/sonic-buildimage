# PW-01 — Chassis LED: Implementation

**STATUS: Complete**

## Changes Made

### `device/accton/x86_64-accton_wedge100s_32x-r0/plugins/led_control.py`

- Added `import os`
- Added `_RUN_DIR = '/run/wedge100s'` constant
- Replaced `_cpld_write(attr, val)` with `_led_write(attr, val)`:
  - Writes `val\n` to `/run/wedge100s/<attr>` (observable state mirror)
  - Then writes `val` to `/sys/bus/i2c/devices/1-0032/<attr>` (hardware)
  - `os.makedirs(_RUN_DIR, exist_ok=True)` guards against missing directory
- All three call sites (`__init__` ×2, `port_link_state_change`) updated automatically

### `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py`

- Added `import os`
- Added `_RUN_DIR = '/run/wedge100s'` class attribute
- Extended `_LED_ENCODE` to include `blue` (0x04) and blink variants
  (`green_blink` 0x0a, `red_blink` 0x09, `blue_blink` 0x0c)
- Replaced `_LED_DECODE` dict-from-reverse-map with an explicit dict covering
  all eight valid CPLD values (0x00–0x0c); eliminates the `amber→red` collision
  that caused `0x01` to decode as `amber` before the `[0x01]='red'` override
- `set_status_led(color)` now writes to `/run/wedge100s/led_sys1` before writing
  to the CPLD sysfs attribute; the sysfs write still determines the return value
- `get_status_led()` still reads from CPLD sysfs (authoritative hardware state)

### `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py`

- Fan speed handler: replaced
  `i2cset -f -y 1 0x32 0x3e 0x02` (SYS1 keep-green side-effect)
  with
  `echo 2 | tee /run/wedge100s/led_sys1 > /sys/bus/i2c/devices/1-0032/led_sys1`
- `set led` handler: replaced two `i2cset` calls (one per register) with a loop
  over `(led_sys1, led_sys2)` using `tee` to write both `/run/wedge100s/<attr>`
  and `/sys/bus/i2c/devices/1-0032/<attr>` in one shell command per attribute

## /run File Format

`/run/wedge100s/led_sys1` and `/run/wedge100s/led_sys2`:
- Plain text, decimal integer, newline-terminated (e.g. `2\n`)
- Permissions: root:root 644 (created by daemon on startup)
- Value encoding: same as CPLD register (0=off, 1=red, 2=green, 4=blue, +8=blink)

## Test stage_09_cpld fix

`_read_int_attr()` and the inline `int(out.strip())` in `test_led_sys2_write_restore`
changed to `int(out.strip(), 0)` to handle the `0x02`-prefixed hex strings the
kernel driver returns.
