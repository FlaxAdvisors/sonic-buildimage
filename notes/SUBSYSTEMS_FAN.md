# Fan Subsystem

## Hardware

- 5 fan trays, each with a **front rotor** and a **rear rotor**
- All fan data lives on the **OpenBMC** I2C bus 8, fan-board controller at `0x33`
- Direction: F2B (front-to-back), fixed per ONL `fani.c` — always `FAN_DIRECTION_INTAKE`
- Maximum RPM: 15,400 (at 100% duty cycle, per `fani.c`)

**BMC sysfs paths** (on the BMC filesystem, not the host):
```
/sys/bus/i2c/devices/8-0033/fantray_present   hex bitmask; bit(N-1) SET = tray N absent
/sys/bus/i2c/devices/8-0033/fan1_input        front rotor, tray 1 (~7500 RPM observed)
/sys/bus/i2c/devices/8-0033/fan2_input        rear  rotor, tray 1 (~4950 RPM observed)
/sys/bus/i2c/devices/8-0033/fan3_input        front rotor, tray 2
/sys/bus/i2c/devices/8-0033/fan4_input        rear  rotor, tray 2
...
/sys/bus/i2c/devices/8-0033/fan9_input        front rotor, tray 5
/sys/bus/i2c/devices/8-0033/fan10_input       rear  rotor, tray 5
```

Fan number formula (matching `fani.c`): tray N → front = `fan(2N-1)_input`, rear = `fan(2N)_input`

Speed control: `set_fan_speed.sh <pct>` sent to BMC via `/dev/ttyACM0`; controls all 5 trays simultaneously.

## Driver / Daemon

- **`wedge100s-bmc-daemon`** (Phase R28): polls BMC over `/dev/ttyACM0` at 57600 baud
  every ~10 s.  Writes per-tray data to `/run/wedge100s/`:
  - `fan_present` — decimal integer (bitmask from `fantray_present`)
  - `fan_1_front` through `fan_5_front` — front rotor RPM
  - `fan_1_rear` through `fan_5_rear` — rear rotor RPM
- No host kernel module reads fan data; all fan hardware is BMC-side.
- Speed control is sent via `bmc.send_command()` which writes a command string to
  the BMC TTY.

## Python API

- **Class `Fan`** in `sonic_platform/fan.py` — inherits `FanBase`; 1-based index 1–5
- **Class `FanDrawer`** in `sonic_platform/fan.py` — inherits `FanDrawerBase`;
  contains one `Fan` object each
- **Instantiated by:** `Chassis.__init__()` creates `FanDrawer(i)` for `i` in 1–5;
  each drawer appends `Fan(i)` to `_fan_list`

| Method | Returns | File read |
|---|---|---|
| `Fan.get_presence()` | `bool` | `/run/wedge100s/fan_present` (bitmask, cached 2 s) |
| `Fan.get_speed_rpm()` | `int` (RPM) or `None` | `/run/wedge100s/fan_N_front`, `/run/wedge100s/fan_N_rear` (cached 2 s) |
| `Fan.get_speed()` | `int` 0–100 (%) | derived from `get_speed_rpm()` ÷ 15400 |
| `Fan.get_target_speed()` | `int` or `NotImplementedError` | module-level `_target_speed_pct` variable |
| `Fan.get_speed_tolerance()` | `20` (%) | hardcoded |
| `Fan.set_speed(speed)` | `bool` | sends `set_fan_speed.sh <pct>` to BMC TTY |
| `Fan.get_direction()` | `FAN_DIRECTION_INTAKE` | hardcoded |
| `Fan.get_status_led()` | `STATUS_LED_COLOR_OFF` | not addressable |
| `FanDrawer.get_presence()` | `bool` | delegates to `Fan.get_presence()` |

**Caching:** `_fantray_cache` (TTL 2 s) and `_rpm_cache` (TTL 2 s, per tray index)
are module-level globals shared across all `Fan` instances.

**`get_target_speed()` behaviour:** raises `NotImplementedError` before the first
`set_speed()` call; this causes `thermalctld`'s `try_get()` to return `NOT_AVAILABLE`,
suppressing false "Not OK" alarms on the first poll cycle.

**Reported speed policy:** `min(front_rpm, rear_rpm)` per tray, matching ONL `fani.c`.
A stalled single rotor drives the tray speed to 0.

## Pass Criteria

- `/run/wedge100s/fan_present` is readable and equals `0` when all 5 trays are installed
- `/run/wedge100s/fan_1_front` through `fan_5_front` contain non-zero integers (e.g. ~7500)
- `/run/wedge100s/fan_1_rear` through `fan_5_rear` contain non-zero integers (e.g. ~4950)
- `Fan(N).get_presence()` returns `True` for all 5 trays
- `Fan(N).get_speed_rpm()` returns a value > 0 for all present trays
- `Fan(N).get_speed()` returns a value in the range 1–100
- `show platform fanstatus` (SONiC CLI) shows `OK` for all 5 trays
- After `Fan(1).set_speed(60)`, `Fan(1).get_target_speed()` returns `60`

## Known Gaps

- Fan tray LEDs are not individually addressable from the host; `set_status_led()`
  and `get_status_led()` always return `False` / `OFF`.
- `get_model()` and `get_serial()` return `'N/A'`; no per-tray identity data is
  available from the BMC fan board controller at `0x33`.
- Speed control applies to all 5 trays simultaneously; per-tray speed is not possible.
- `set_speed()` succeeds or fails atomically for all trays; no partial-tray feedback.
- The daemon writes `fan_present` as a decimal integer from `fantray_present`; if the
  BMC sysfs file uses a hex prefix the daemon must strip it (verify daemon behaviour
  against live hardware if presence reads return unexpected values).
- No low-speed threshold alarm is raised by the Python API itself; `thermalctld`
  handles is_under_speed using `get_speed()`, `get_target_speed()`, and
  `get_speed_tolerance()`.
