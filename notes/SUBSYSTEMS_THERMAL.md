# Thermal Subsystem

## Hardware

8 thermal sensors total; index 0 is on the host, indices 1–7 are all BMC-side.

| Index | Name | Device | Location |
|---|---|---|---|
| 0 | CPU Core | Intel coretemp (host kernel) | Host CPU (Broadwell-DE D1508) |
| 1 | TMP75-1 | TI TMP75 | BMC i2c-3, addr `0x48` |
| 2 | TMP75-2 | TI TMP75 | BMC i2c-3, addr `0x49` |
| 3 | TMP75-3 | TI TMP75 | BMC i2c-3, addr `0x4a` |
| 4 | TMP75-4 | TI TMP75 | BMC i2c-3, addr `0x4b` |
| 5 | TMP75-5 | TI TMP75 | BMC i2c-3, addr `0x4c` |
| 6 | TMP75-6 | TI TMP75 | BMC i2c-8, addr `0x48` |
| 7 | TMP75-7 | TI TMP75 | BMC i2c-8, addr `0x49` |

All 7 TMP75 sensors are wired to the **BMC** I2C bus, not the host. They are not
reachable from the host filesystem; no host kernel `lm75` driver is loaded for these.

Verified live readings (hardware, 2026-02-25):
TMP75-1: 23.75 °C, TMP75-2: 22.9 °C, TMP75-3: 23.1 °C, TMP75-4: 33.3 °C,
TMP75-5: 21.1 °C, TMP75-6: 20.6 °C, TMP75-7: 23.0 °C.

## Driver / Daemon

**Index 0 — CPU Core:**
- Read directly by Python via the host `coretemp` kernel module
- Sysfs glob: `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input`
- Values are in millidegrees C; divided by 1000 to get °C
- `max()` is taken across all matched files (mirrors `onlp_file_read_int_max()`)

**Indices 1–7 — TMP75 sensors:**
- **`wedge100s-bmc-daemon`** (Phase R28): polls all 7 TMP75 sensors via `/dev/ttyACM0`
  (BMC TTY at 57600 baud); writes millidegrees C as plain decimal integers to:
  - `/run/wedge100s/thermal_1` through `/run/wedge100s/thermal_7`
- No host kernel driver; no direct I2C path from host to TMP75 devices.

## Python API

- **Class:** `Thermal` in `sonic_platform/thermal.py` — inherits `ThermalBase`; 0-based index 0–7
- **Instantiated by:** `Chassis.__init__()` as `Thermal(i)` for `i` in range 8

| Method | Returns | Source |
|---|---|---|
| `get_name()` | e.g. `'CPU Core'`, `'TMP75-4'` | `_SENSORS` table |
| `get_temperature()` | `float` (°C) or `None` | see below |
| `get_high_threshold()` | `float` (°C) | `_SENSORS` table (95.0 for CPU, 70.0 for TMP75) |
| `get_high_critical_threshold()` | `float` (°C) | `_SENSORS` table (102.0 for CPU, 80.0 for TMP75) |
| `set_high_threshold(t)` | `True` | updates in-object `_high` |
| `set_high_critical_threshold(t)` | `True` | updates in-object `_high_crit` |
| `get_minimum_recorded()` | `float` or `None` | in-object `_min_recorded`, updated on each `get_temperature()` |
| `get_maximum_recorded()` | `float` or `None` | in-object `_max_recorded`, updated on each `get_temperature()` |
| `get_presence()` | `True` (always) | board-mounted; always present |
| `get_status()` | `bool` | `_read_temperature() is not None` |

**Read paths:**
- Source `"host"` (index 0): globs coretemp path, reads all files, returns max / 1000.0
- Source `"daemon"` (indices 1–7): reads `/run/wedge100s/thermal_N` as decimal integer
  and divides by 1000.0

**Threshold values from `_SENSORS` table:**
- CPU Core: high = 95 °C, high-critical = 102 °C
- TMP75-1 through TMP75-7: high = 70 °C, high-critical = 80 °C

## Pass Criteria

- `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp1_input` is readable and
  returns a value in the range 20,000–95,000 (millidegrees C)
- `Thermal(0).get_temperature()` returns a float in ~20–80 °C range
- `/run/wedge100s/thermal_1` through `thermal_7` are all readable
- All daemon files contain decimal integers in the range 15000–85000 (millidegrees)
- `Thermal(N).get_temperature()` for N in 1–7 returns a float in ~15–85 °C range
- `Thermal(N).get_status()` returns `True` for all 8 sensors
- `show platform temperature` (SONiC CLI) lists all 8 sensors with non-zero readings

## Known Gaps

- Thresholds are compile-time constants in `_SENSORS` table; they cannot be updated
  persistently (writes to `_high` / `_high_crit` are per-object, lost on `pmon` restart).
- No low-temperature threshold or low-critical threshold is defined; `get_low_threshold()`
  and `get_low_critical_threshold()` are not overridden and will raise `NotImplementedError`
  if called.
- The coretemp glob uses `hwmon*` — on kernels where the hwmon index is non-zero the
  glob resolves correctly, but the pattern is fragile if multiple hwmon devices exist with
  the same `temp*_input` name prefix.
- `get_model()` and `get_serial()` return `'N/A'` (board-mounted sensors have no identity).
- Min/max recorded values are in-process state only; they reset to `None` on every
  `pmon` restart. No persistence to REDIS STATE_DB.
- TMP75 sensor names (TMP75-1 through TMP75-7) do not indicate physical board location;
  correlate against ONL `thermali.c` or hardware schematic to identify which sensor
  monitors which component.
