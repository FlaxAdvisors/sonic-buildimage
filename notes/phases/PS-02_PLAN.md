# PS-02 PLAN — Fan Subsystem

## Problem Statement

SONiC's `thermalctld` daemon iterates `chassis.get_all_fan_drawers()` and calls
`drawer.get_all_fans()` to determine fan health. The Wedge 100S-32X has 5 fan
trays, each with a front rotor and a rear rotor. Without a `Fan`/`FanDrawer`
implementation, `thermalctld` has no fan telemetry and cannot close the
thermal control loop.

Fan speed control is owned by OpenBMC (fan board controller at i2c-8/0x33).
The host CPU cannot directly drive PWM — it must send a `set_fan_speed.sh`
command to the BMC over the TTY serial link.

## Proposed Approach

**Presence and RPM data:** The `wedge100s-bmc-daemon` polls the BMC every 10 s
and writes:
- `/run/wedge100s/fan_present` — `fantray_present` bitmask as decimal integer
  (0 = all present; bit N set = tray N+1 absent)
- `/run/wedge100s/fan_N_front` — front rotor RPM for tray N (1-based)
- `/run/wedge100s/fan_N_rear` — rear rotor RPM for tray N (1-based)

**FanDrawer model:** Each of the 5 fan trays is modeled as a `FanDrawer`
containing a single `Fan` object. The `Fan` reports `min(front_rpm, rear_rpm)`
as its speed — matching ONL `fani.c` policy that any stalled rotor fails the
tray. The model is 5 drawers × 1 fan each = 5 `Fan` objects total.

**Speed control:** `Fan.set_speed(pct)` calls `bmc.send_command('set_fan_speed.sh N')`
which sends the command over `/dev/ttyACM0`. All 5 trays share one speed target
because the BMC controls them jointly.

**Cache:** Both the presence bitmask and per-tray RPM pairs are cached for
2 seconds at module level to avoid redundant daemon file reads when `thermalctld`
reads multiple attributes of the same fan in a single poll pass.

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/fan.py` | FanDrawer and Fan classes |

`chassis.py` must call `FanDrawer(i)` for `i` in `range(1, NUM_FANS + 1)`.

## Acceptance Criteria

- `Chassis().get_all_fan_drawers()` returns exactly 5 `FanDrawer` objects
- Each drawer contains exactly 1 `Fan` object
- All 5 fans return `get_presence() == True` when all trays are installed
- All 5 fans return `get_speed_rpm() > 0` under normal operation
- `get_direction()` returns `FAN_DIRECTION_INTAKE` for all fans
- `get_target_speed()` raises `NotImplementedError` before first `set_speed()`
  call (this is intentional, not a bug — prevents false "Not OK" alarms)

## Risks and Watchouts

- **bmc-poller not running:** Missing `/run/wedge100s/fan_present` causes
  `get_presence()` to return `False` for all fans. `thermalctld` will alarm.
- **Max RPM constant:** `_MAX_FAN_SPEED = 15400` (from ONL `fani.c`). If actual
  max RPM differs (e.g. after a fan replacement with different model), the
  percentage calculation will be wrong. RPM is still valid even if percentage
  is off.
- **pmon container access to ttyACM0:** `set_speed()` requires `/dev/ttyACM0`
  to be passed into the pmon Docker container via `--device`. The postinst
  patches `pmon.sh` to add this. Without it, `bmc.send_command()` fails and
  `set_speed()` returns `False`.
- **NotImplementedError for target speed:** `thermalctld`'s `try_get()` wrapper
  catches `NotImplementedError` and substitutes `NOT_AVAILABLE`, skipping the
  is_under/over_speed check. This is the intended behavior on first boot.
