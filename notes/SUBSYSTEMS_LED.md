# LED Subsystem

## Hardware

Two system LEDs, both controlled via CPLD at host `i2c-1` / `0x32`:

| LED | CPLD Register | Owner | Function |
|---|---|---|---|
| SYS1 | `0x3e` | `chassis.py` / `healthd` | System-status indicator |
| SYS2 | `0x3f` | `led_control.py` / `ledd` | Port-activity indicator |

**Register encoding** (from ONL `ledi.c`, verified in CPLD driver):
```
0x00 = off           0x08 = off (blinking)
0x01 = red           0x09 = red blinking
0x02 = green         0x0a = green blinking
0x04 = blue          0x0c = blue blinking
```

The hardware has no amber LED; `chassis.py` maps `'amber'` to `0x01` (red).

## Driver / Daemon

- **`wedge100s_cpld`** kernel module (Phase R26): exposes `led_sys1` and `led_sys2`
  as read-write sysfs attributes at `/sys/bus/i2c/devices/1-0032/`.
- Writes use `kstrtoul(buf, 0, &val)` so both decimal (`2`) and hex (`0x02`) strings
  are accepted.
- Reads return the current register value as `0x%02x\n`.
- The driver uses `i2c_smbus_write_byte_data` with up to 10 retries at 60 ms intervals.

**ledd** (SONiC daemon): calls `LedControl.port_link_state_change()` on port state
transitions. SYS2 is managed exclusively by `led_control.py`.

**healthd** (SONiC daemon): calls `Chassis.set_status_led()` to set SYS1.

## Python API

### chassis.py — SYS1 (system-status LED)

File: `sonic_platform/chassis.py`

| Method | Colour encoding | Sysfs path |
|---|---|---|
| `set_status_led(color)` | `'green'→0x02`, `'red'→0x01`, `'amber'→0x01`, `'off'→0x00` | `/sys/bus/i2c/devices/1-0032/led_sys1` |
| `get_status_led()` | returns canonical colour string | `/sys/bus/i2c/devices/1-0032/led_sys1` |

Returns `False` / `STATUS_LED_COLOR_OFF` on any sysfs access failure.

### led_control.py — SYS2 (port-activity LED)

File: `device/accton/x86_64-accton_wedge100s_32x-r0/plugins/led_control.py`
Class: `LedControl(LedControlBase)`

| Method | Action |
|---|---|
| `__init__()` | Sets SYS1 = green; reads STATE_DB `PORT_TABLE` for initial port states; sets SYS2 = green if any port is up, else off |
| `port_link_state_change(port, state)` | Updates `_port_states[port]`; sets SYS2 = green if any port up, else off |

**STATE_DB initial scan:** on init, `LedControl` reads all `PORT_TABLE` entries from
STATE_DB and pre-populates `_port_states`. This prevents SYS2 staying off after a
`pmon` restart when ports are already up (ledd only fires on transitions, not on init).

**SYS2 logic:** binary — green when at least one port has `netdev_oper_status == 'up'`,
off otherwise. No per-port LED hardware exists.

## Pass Criteria

- `/sys/bus/i2c/devices/1-0032/led_sys1` is readable and writable
- `Chassis.set_status_led('green')` returns `True`
- `cat /sys/bus/i2c/devices/1-0032/led_sys1` returns `0x02` after setting green
- `Chassis.get_status_led()` returns `'green'`
- After `pmon` starts, SYS1 is physically green on the switch front panel
- When at least one port is link-up, SYS2 is physically green
- Writing `echo 1 > /sys/bus/i2c/devices/1-0032/led_sys2` turns SYS2 red

## Known Gaps

- No amber LED hardware; `'amber'` is silently mapped to red (`0x01`). The `_LED_DECODE`
  dict canonicalises `0x01` back to `'red'`, so a round-trip of `set('amber')` then
  `get()` returns `'red'`.
- Fan tray LEDs are not individually addressable; `Fan.set_status_led()` and
  `FanDrawer.set_status_led()` always return `False`.
- PSU `set_status_led()` always returns `False`; LED state for PSUs is synthesized
  from `get_status()` only for the API response, not written to hardware.
- `LedControl.__init__()` writes SYS1 = green regardless of system health; `healthd`
  is expected to subsequently set SYS1 to the appropriate colour.
- Blue LED (`0x04`) and blink variants (`+0x08`) are supported by hardware and the CPLD
  driver but are not used by any current Python code.
- No CPLD-level interrupt or interrupt-driven LED change; all updates are explicit writes.
