# PS-06 PLAN — LED Control

## Problem Statement

The Wedge 100S-32X front panel has two system LEDs (SYS1, SYS2) controlled via
CPLD registers 0x3e and 0x3f at i2c-1/0x32. SONiC has two separate components
that need to drive these LEDs:

1. **`healthd`** calls `chassis.set_status_led()` to reflect overall system
   health (SYS1: green = running, red = degraded).
2. **`ledd`** uses `led_control.py` as a plugin to set SYS2 based on port
   link state.

Without LED control, the front panel LEDs remain in their post-boot state
(typically off) regardless of system health or port state.

## Proposed Approach

**SYS1 (reg 0x3e) — system-status LED:**
Owned by `chassis.py` via `set_status_led()` / `get_status_led()`. Writes to
`/sys/bus/i2c/devices/1-0032/led_sys1` sysfs attribute exposed by the
`wedge100s_cpld` driver.

Color encoding from `ledi.c`:
- `0x00` = off
- `0x01` = red (also used for amber — hardware has no amber)
- `0x02` = green
- `0x04` = blue
- `+0x08` = blink modifier

**SYS2 (reg 0x3f) — port-activity LED:**
Owned by `led_control.py` plugin loaded by `ledd`. On init, reads current port
states from STATE_DB to handle the case where ports are already up when `ledd`
starts. On each `port_link_state_change()` callback, updates SYS2 to green if
any port is up, off otherwise.

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py` | `set_status_led()`, `get_status_led()` |
| `device/accton/x86_64-accton_wedge100s_32x-r0/plugins/led_control.py` | `LedControl` plugin for ledd |

## Acceptance Criteria

- `chassis.set_status_led('green')` writes `2` to `led_sys1` sysfs attribute
- `chassis.set_status_led('red')` writes `1`
- `chassis.get_status_led()` returns the previously set color
- `LedControl.__init__()` sets SYS1 to green and SYS2 based on current port states
- `LedControl.port_link_state_change(port, 'up')` sets SYS2 to green
- `LedControl.port_link_state_change(port, 'down')` when all ports down sets SYS2 to off

## Risks and Watchouts

- **CPLD module required:** Both `chassis.py` and `led_control.py` write to
  `/sys/bus/i2c/devices/1-0032/`. If `wedge100s_cpld` is not loaded or the
  device is not registered, writes are silently ignored (wrapped in `try/except`).
- **No amber:** The hardware has no amber LED. The `chassis.py` color map
  translates `'amber'` to `0x01` (red). `_LED_DECODE` always returns `'red'`
  for `0x01`, so a `set_status_led('amber')` followed by `get_status_led()`
  returns `'red'`, not `'amber'`. This is correct behavior.
- **SYS2 initial state race:** If `ledd` starts before STATE_DB is populated
  (first boot), `_state_db_port_states()` returns `{}` and SYS2 starts off.
  As ports come up, `port_link_state_change()` callbacks update it correctly.
- **ledd restart after ports up:** If `ledd` restarts while ports are already
  up, the `_state_db_port_states()` initial scan ensures SYS2 is correctly lit
  without waiting for link-state transitions.
