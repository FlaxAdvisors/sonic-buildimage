# PS-01 IMPLEMENTATION — Thermal Subsystem

## Files Changed

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/thermal.py`
  (new file, Phase R28/R29 era)

## Sensor Table

Defined in `_SENSORS` list at module level. All thresholds are `float` (°C).

| Index | Name | Source | Path | High (°C) | High Crit (°C) |
|---|---|---|---|---|---|
| 0 | CPU Core | host | `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input` | 95.0 | 102.0 |
| 1 | TMP75-1 | daemon | `/run/wedge100s/thermal_1` | 70.0 | 80.0 |
| 2 | TMP75-2 | daemon | `/run/wedge100s/thermal_2` | 70.0 | 80.0 |
| 3 | TMP75-3 | daemon | `/run/wedge100s/thermal_3` | 70.0 | 80.0 |
| 4 | TMP75-4 | daemon | `/run/wedge100s/thermal_4` | 70.0 | 80.0 |
| 5 | TMP75-5 | daemon | `/run/wedge100s/thermal_5` | 70.0 | 80.0 |
| 6 | TMP75-6 | daemon | `/run/wedge100s/thermal_6` | 70.0 | 80.0 |
| 7 | TMP75-7 | daemon | `/run/wedge100s/thermal_7` | 70.0 | 80.0 |

Hardware mapping (from ONL `thermali.c`):
- TMP75-1 through TMP75-5: BMC i2c-3 at addresses 0x48–0x4c (mainboard)
- TMP75-6, TMP75-7: BMC i2c-8 at addresses 0x48, 0x49 (fan board)

## Key Decisions

**CPU Core reads max across all cores.** `_read_host_temp_max()` globs all
`temp*_input` files and returns the maximum. This matches ONL's
`onlp_file_read_int_max()` for Broadwell multi-core CPUs where per-core
temperatures can differ by several degrees.

**Daemon files contain millidegrees.** `_read_daemon_temp()` divides by 1000.
The format is a plain decimal integer (e.g. `23750` for 23.75 °C) written by
`wedge100s-bmc-daemon` after parsing the BMC's `thd-util get temp` output.

**`get_status()` is False when unreadable.** Returns `_read_temperature() is not None`.
This means a missing daemon file causes the sensor to report failed, not just
reading 0. `thermalctld` handles `None` from `get_temperature()` via `try_get()`.

**Min/max recorded are in-process only.** `_min_recorded` and `_max_recorded`
are instance variables reset on each pmon container start. They are not persisted
to disk.

**`is_replaceable()` returns `False`** — all sensors are board-mounted and
not hot-swappable.

**`set_high_threshold()` and `set_high_critical_threshold()` are writable.**
They update the instance variable. This allows `thermalctld` to apply
platform-level policies at runtime without requiring code changes.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64, 2026-02-25):
- TMP75-1 (3-0048): 23.75 °C
- TMP75-2 (3-0049): 22.9 °C
- TMP75-3 (3-004a): 23.1 °C
- TMP75-4 (3-004b): 33.3 °C (warmest mainboard sensor, near PSU area)
- TMP75-5 (3-004c): 21.1 °C
- TMP75-6 (8-0048): 20.6 °C
- TMP75-7 (8-0049): 23.0 °C

All 7 BMC sensors returned valid readings from `/run/wedge100s/thermal_N` files.
CPU Core sensor: coretemp driver present, readings in 45–60 °C range under load.

## Remaining Known Gaps

- No `get_low_threshold()` / `get_low_critical_threshold()` implementation
  (returns `None` from base class). Not required by `thermalctld`.
- `get_model()` and `get_serial()` return `'N/A'` — TMP75 sensors have no
  identity registers accessible from the BMC API.
- If the BMC TTY is locked by another process, the daemon may skip writes,
  causing stale files. There is no staleness check in `thermal.py`; the
  daemon-level cache TTL is 10 s (bmc-poller.timer).
