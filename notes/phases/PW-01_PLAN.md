# PW-01 — Chassis LED: Plan

## Problem Statement

The Wedge 100S-32X CPLD exposes two LED registers:

| Register | Attribute | Owner | Purpose |
|---|---|---|---|
| 0x3e | `led_sys1` | `chassis.py` via `set_status_led()` | System-status indicator; healthd drives this green/red |
| 0x3f | `led_sys2` | `led_control.py` via `LedControl` | Port-activity indicator; ledd drives green when any port is up |

The chassis has exactly **two** front-panel LED positions wired to the CPLD. The ONL `ledi.c`
for the Wedge 100S defines only `LED_SYS1` and `LED_SYS2`. No additional LED registers exist
in the CPLD register map (verified against `wedge100s_cpld.c`).

**Finding: All hardware LED positions are already implemented.** Both LED positions are
fully wired:
- SYS1 — read/write via `led_sys1` sysfs attribute, driven by `chassis.py`
- SYS2 — read/write via `led_sys2` sysfs attribute, driven by `led_control.py`

There is no unimplemented LED position in the CPLD.

## Quality Gaps

### Gap 1: `chassis.py` ignores blue and blink encoding

`chassis.py` maps `amber → red` (the hardware has no amber LED). The CPLD register supports
`0x04` (blue) and the `+0x08` blink modifier. The `_LED_DECODE` dict only covers `{0x00, 0x01,
0x02}`. If a future caller writes `0x04` (blue) directly to `led_sys1`, `get_status_led()`
returns `STATUS_LED_COLOR_OFF` instead of the correct color name.

### Gap 2: SYS1 is never set red on unhealthy state

`healthd` calls `set_status_led(RED)` when a critical sensor threshold is exceeded.
The encoding maps `red → 0x01` and the write path works. However, if `healthd` is not running
(which is common in SONiC on platforms that disable it), SYS1 stays green permanently.
This is a process-level gap, not a chassis API gap.

### Gap 3: `accton_wedge100s_util.py` writes LEDs via raw `i2cset`

The CLI `set led` command and the fan-speed handler both call `i2cset -f -y 1 0x32 0x3e/0x3f`
directly, bypassing the `wedge100s_cpld` kernel driver. This causes a race with any concurrent
sysfs write (e.g. from ledd or healthd) because `i2cset` does not go through the driver's lock.
The fan-speed handler additionally forces SYS1 to green via `i2cset` as a side-effect.

### Gap 4: LED state not observable without touching the i2c bus

No file in `/run/wedge100s` reflects the current LED state. Checking LED state requires either
`cat /sys/bus/i2c/devices/1-0032/led_sysN` (sysfs, goes to driver) or `i2cget` (bypasses
driver). A `/run` mirror file enables fast, race-free reads from any tool.

## Proposed Approach

### Write-through /run mirror

All LED writers follow the same pattern:

1. Write the decimal value to `/run/wedge100s/led_sys1` or `/run/wedge100s/led_sys2`
2. Write the same value to the CPLD sysfs attribute

The `/run` file is the observable state cache. The sysfs write is the hardware application.
Both happen atomically within the same call. No polling thread or daemon coordination is
needed — the kernel driver serializes concurrent sysfs writes.

This eliminates the `i2cset` race (Gap 3) and adds observability (Gap 4) without changing
the ownership model (ledd owns SYS2; chassis.py/healthd owns SYS1).

### Files to Change

| File | Change |
|---|---|
| `device/.../plugins/led_control.py` | Replace `_cpld_write()` with `_led_write()` that writes /run + sysfs |
| `sonic_platform/chassis.py` | `set_status_led()` writes /run + sysfs; extend encode/decode for blue + blink (Gap 1) |
| `utils/accton_wedge100s_util.py` | Replace `i2cset` calls with `tee /run/wedge100s/ledN > /sys/.../ledN` |

### /run file format

Plain text, one line: the decimal integer value followed by a newline (e.g. `2\n` for green).
Readers use `int(val.strip(), 0)` to handle either decimal or `0x`-prefixed hex.

## Acceptance Criteria

- `set_status_led('green')` → LED shows green; `get_status_led()` returns `'green'`
- `set_status_led('red')` → LED shows red; `get_status_led()` returns `'red'`
- `set_status_led('off')` → LED off; `get_status_led()` returns `'off'`
- `set_status_led('blue')` → `get_status_led()` returns `'blue'` (not `'off'`)
- After any `set_status_led()` call, `/run/wedge100s/led_sys1` contains the new value
- `accton_wedge100s_util.py set led <N>` updates `/run/wedge100s/led_sys1` and `led_sys2`
  without calling `i2cset`
- `led_control.py` (ledd) writes `/run/wedge100s/led_sys2` on init and on every port state change
- No regression to ledd-driven SYS2 behavior

## Risks

- **Low risk.** The /run write-through is additive; the existing sysfs write path is unchanged.
- Do not change SYS2 ownership. SYS2 is owned by `ledd` via `led_control.py`.
- The SONiC `ChassisBase` API only defines one system LED (`set_status_led`). There is no
  API for SYS2 from `chassis.py`; SYS2 is correctly accessed via the `LedControlBase` plugin.
