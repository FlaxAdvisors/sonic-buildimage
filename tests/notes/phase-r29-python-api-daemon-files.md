# Phase R29 — Python API → sysfs/Daemon Files

**Completed: 2026-03-11**

## What changed

All BMC sensor reads in the Python platform API now read from
`/run/wedge100s/` files written by `wedge100s-bmc-daemon` (R28) instead
of issuing per-call TTY sessions via `bmc.py`.

### thermal.py
- `_source` for TMP75 sensors changed from `'bmc'` to `'daemon'`
- `_path` for TMP75 sensors changed from BMC sysfs paths to `/run/wedge100s/thermal_{1..7}`
- `_read_bmc_temp()` → `_read_daemon_temp()`: reads millidegrees C directly from file
- `bmc` import removed entirely

### fan.py
- Added `_daemon_read_int(path)` helper — reads a plain decimal integer from a file
- `_cached_fantray_present()`: now reads `/run/wedge100s/fan_present` (decimal int)
  instead of `bmc.file_read_int(..., base=16)`
- `_cached_rpm_pair()`: now reads `/run/wedge100s/fan_{N}_front` and `fan_{N}_rear`
  instead of `bmc.file_read_int()` calls
- `bmc` import retained — `set_speed()` still calls `bmc.send_command('set_fan_speed.sh <pct>')`

### psu.py
- Removed: `_set_bmc_mux()`, `_BMC_MUX_BUS`, `_BMC_MUX_ADDR`, `_PSU_BMC`,
  `_REG_VIN/IIN/IOUT/POUT` constants
- Added `_read_daemon_int(path)` helper
- `_read_psu_telemetry()`: reads raw LINEAR11 words from
  `/run/wedge100s/psu_{N}_{vin,iin,iout,pout}` and decodes via
  `_pmbus_decode_linear11()` (unchanged)
- `bmc` import removed entirely

### bmc.py
- Removed: `_parse_int()`, `file_read_int()`, `i2cget_byte()`, `i2cget_word()`,
  `i2cset_byte()`
- Retained: `send_command()` and all TTY helpers (used by fan `set_speed()`)

## Daemon file format

All files in `/run/wedge100s/` are plain decimal integers followed by `\n`.
Written atomically by `wedge100s-bmc-daemon` via `fopen/fprintf/fclose`.

| File | Content | Consumer |
|---|---|---|
| `thermal_{1..7}` | millidegrees C | thermal.py |
| `fan_present` | bitmask (0=all present) | fan.py |
| `fan_{1..5}_front` | RPM | fan.py |
| `fan_{1..5}_rear` | RPM | fan.py |
| `psu_{1,2}_{vin,iin,iout,pout}` | raw LINEAR11 word | psu.py |

## What still uses the TTY

`fan.set_speed()` → `bmc.send_command('set_fan_speed.sh <pct>')` — this is the
only remaining write path to the BMC.  The daemon is read-only.

## Hardware verification needed

1. Confirm `wedge100s-bmc-poller.timer` is running and files exist:
   ```bash
   ls -la /run/wedge100s/
   cat /run/wedge100s/thermal_1
   cat /run/wedge100s/fan_1_front
   cat /run/wedge100s/psu_2_pout
   ```
2. Restart pmon and confirm thermalctld poll cycle completes in ~10s (not ~65s):
   ```bash
   sudo systemctl restart pmon
   docker exec pmon supervisorctl status thermalctld
   ```
