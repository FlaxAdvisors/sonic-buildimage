# PS-01 PLAN — Thermal Subsystem

## Problem Statement

SONiC's `thermalctld` daemon (inside pmon) requires a `Thermal` class that
implements `ThermalBase` for every sensor on the platform. The Wedge 100S-32X
has 8 temperature sensors:

- 1 CPU Core sensor — readable directly from host via coretemp hwmon sysfs
- 7 TMP75 sensors on the OpenBMC I2C bus — not accessible from the host CPU

Without this class, `thermalctld` cannot monitor temperatures, leaving SONiC
with no thermal protection and `show environment` reporting no sensors.

## Proposed Approach

**CPU Core (index 0):** Use Python `glob` to expand the wildcard path
`/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input` and return the
maximum reading across all cores. This matches ONL's `onlp_file_read_int_max()`
pattern.

**TMP75 sensors (indices 1–7):** Read from files written by the
`wedge100s-bmc-daemon` process at `/run/wedge100s/thermal_N`. The daemon polls
the BMC over `/dev/ttyACM0` (57600 baud) and writes millidegrees C as plain
decimal integers. The Python class divides by 1000 to get °C.

This split avoids the need for host-side access to BMC I2C buses.

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/thermal.py` | New Thermal class |

`chassis.py` must instantiate `Thermal(i)` for `i` in `range(NUM_THERMALS)`.

## Acceptance Criteria

- `Chassis().get_all_thermals()` returns exactly 8 `Thermal` objects
- `get_temperature()` returns a `float` for all 8 sensors when the system is
  running normally (bmc-poller timer active, coretemp driver loaded)
- `get_high_threshold()` returns a non-`None` float for every sensor
- `get_high_critical_threshold()` returns a non-`None` float for every sensor
- `get_name()` returns distinct strings for all 8 sensors
- `get_presence()` returns `True` for all sensors (board-mounted)

## Risks and Watchouts

- **bmc-poller not running:** If `wedge100s-bmc-poller.timer` is not active,
  `/run/wedge100s/thermal_N` will be absent or stale. `get_temperature()`
  returns `None`; `thermalctld` will raise `NOT_AVAILABLE` alarms.
- **coretemp driver absent:** Possible if the kernel is built without
  `CONFIG_X86_PLATFORM_DEVICES`. The glob returns an empty list and CPU Core
  returns `None`. Verify coretemp is loaded before running tests.
- **thermalctld false "Not OK" alarms:** If `get_temperature()` briefly returns
  `None` at startup (daemon files not yet written), thermalctld may log alarm
  transitions. This is transient and clears within 10 s once the daemon writes
  its first files.
- **Broadwell Tjmax:** CPU Core thresholds (95.0 / 102.0 °C) are set for the
  Broadwell-DE D1508 Tjmax ≈ 105 °C. Do not lower these without checking the
  CPU datasheet.
- **hwmon index change:** `hwmon*` glob is required because the hwmon enumeration
  index changes between boots. A fixed path like `hwmon1` would fail.
