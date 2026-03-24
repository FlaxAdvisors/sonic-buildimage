# I2C Isolation: All pmon/ledd/CLI access moved to /run/wedge100s/

**Date:** 2026-03-23
**Branch:** wedge100s

## Goal

Eliminate all direct I2C/sysfs bus access from Python code running in or
alongside pmon.  Only the two host-side daemons (`wedge100s-i2c-daemon`,
`wedge100s-bmc-daemon`) are permitted to touch the I2C bus.

## Motivation

- The CP2112 USB-I2C bridge can hang permanently if a process is SIGKILL'd
  mid-ioctl (e.g. `docker rm -f pmon` while xcvrd is running).  Recovery
  requires a power cycle.
- The PCA9548 mux tree shared between the system EEPROM (ch6) and the
  PCA9535 QSFP presence chips (ch2/3) caused address-corruption and
  zeroed-data incidents when accessed concurrently.
- Centralising I2C ownership in single-threaded daemons eliminates all
  contention and lockup risk.

## Architecture after this change

All runtime I2C state is available under `/run/wedge100s/` as plain files.
Python code reads and writes those files only.

| File(s) | Writer | Consumers |
|---|---|---|
| `sfp_{N}_present` | `wedge100s-i2c-daemon` (PCA9535 poll, 3 s) | `sfp.py`, `chassis.py` |
| `sfp_{N}_eeprom` | `wedge100s-i2c-daemon` (on insertion) | `sfp.py` |
| `sfp_{N}_lpmode` / `_lpmode_req` | daemon ↔ `sfp.py` | `sfp.py` |
| `sfp_{N}_read_req` / `_read_resp` | `sfp.py` → daemon | `sfp.py` |
| `sfp_{N}_write_req` / `_write_ack` | `sfp.py` → daemon | `sfp.py` |
| `syseeprom` | `wedge100s-i2c-daemon` (once at boot) | `eeprom.py` |
| `cpld_version` | `wedge100s-i2c-daemon` `poll_cpld()` (3 s) | `component.py` |
| `psu{1,2}_present`, `psu{1,2}_pgood` | `wedge100s-i2c-daemon` `poll_cpld()` (3 s) | `psu.py` |
| `led_sys1`, `led_sys2` | `chassis.py`, `led_control.py`, `accton_wedge100s_util.py` (write) → `wedge100s-i2c-daemon` `apply_led_writes()` (3 s push to CPLD) | `chassis.py`, `led_control.py` |
| `thermal_{1-7}` | `wedge100s-bmc-daemon` (10 s) | `thermal.py` |
| `fan_present`, `fan_{N}_{front,rear}` | `wedge100s-bmc-daemon` (10 s) | `fan.py` |
| `psu_{1,2}_{vin,iin,iout,pout}` | `wedge100s-bmc-daemon` (10 s) | `psu.py` |

The CPU Core temperature sensor (`thermal.py` index 0) reads
`/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input` directly — this
is the x86 MSR-based `coretemp` driver, not I2C, and is safe.

## Files changed

### `utils/wedge100s-i2c-daemon.c`

**`poll_cpld()`** — removed `led_sys1` and `led_sys2` from the
read-then-mirror loop.  Those attrs are now write-through (pmon owns the
desired state; the daemon pushes it to hardware).  Remaining attrs mirrored:
`cpld_version`, `psu1_present`, `psu1_pgood`, `psu2_present`, `psu2_pgood`.

**`apply_led_writes()`** — new function, called before `poll_cpld()` each
tick:
- **Seed path** (file absent, first tick after boot): reads hardware state
  from CPLD sysfs and writes `/run/wedge100s/led_sys{1,2}` so
  `get_status_led()` returns the correct value before any `set_status_led()`
  call.
- **Write-through path** (file present): reads value from `/run/wedge100s/`
  and writes to `/sys/bus/i2c/devices/1-0032/led_sys{1,2}`.  Idempotent —
  writing the same value each tick is harmless.

Call order in main loop:
```
apply_led_writes();   // seed or write-through before read-only mirror
poll_cpld();          // mirror cpld_version, psuN_present/pgood to /run/
```

Both must run after the hidraw block closes: each CPLD sysfs access via the
kernel hid_cp2112 driver leaves 2 stale HID reports in the buffer; running
after hidraw is closed means those reports are drained by `cp2112_cancel()`
at the start of the next tick.

### `sonic_platform/chassis.py`

- Removed `_CPLD_SYSFS` class constant.
- `set_status_led()`: writes only to `/run/wedge100s/led_sys1`; returns
  `True` on success.  Daemon handles the CPLD write within one 3 s tick.
- `get_status_led()`: reads only from `/run/wedge100s/led_sys1`; no sysfs
  fallback.

### `sonic_platform/psu.py`

- Removed `_CPLD_SYSFS` module constant.
- `_read_cpld_attr()`: single read from `_RUN_DIR`; returns `None` on any
  error.  Removed fallback loop to CPLD sysfs.

### `sonic_platform/component.py`

- `CPLD_VERSION_PATH`: changed from
  `/sys/bus/i2c/devices/1-0032/cpld_version` to
  `/run/wedge100s/cpld_version`.

### `sonic_platform/eeprom.py`

- Removed `_SYSEEPROM_SYSFS` constant.
- `read_eeprom()`: daemon cache only; returns `None` when file absent.  No
  sysfs fallback.
- `get_eeprom()`: **only caches on successful parse** (`result` non-empty).
  When the daemon file is absent (normal during the first few seconds after
  boot), returns `{}` without caching so the next call retries.  Once the
  file arrives and parses correctly the result is cached permanently (EEPROM
  content is static).
- `super().__init__('')`: passes empty path — the base-class path field
  (`self.p`) is never used because `read_eeprom()` is fully overridden.

### `device/.../plugins/led_control.py`

- Removed `_CPLD_SYSFS` constant.
- `_led_write()`: writes only to `/run/wedge100s/{attr}`; removed direct
  CPLD sysfs write.  The daemon pushes the value to hardware on its next
  tick (~3 s).  Acceptable latency for a status/activity LED.

### `utils/accton_wedge100s_util.py`

Two `tee`-based commands that wrote to both `/run/wedge100s/` and CPLD sysfs
directly:

1. Fan-speed CLI shim (line ~569): `tee /run/wedge100s/led_sys1 > /sys/.../led_sys1`
   → changed to `echo 2 > /run/wedge100s/led_sys1`.
2. `led` CLI command (lines ~588-591): loop over led_sys1/led_sys2 with `tee`
   → changed to `echo {val} > /run/wedge100s/{attr}`.

## Remaining legitimate /sys access in pmon

| Path | File | Reason |
|---|---|---|
| `/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input` | `thermal.py` | x86 `coretemp` MSR driver — not I2C |
| `/sys/bus/i2c/devices/1-0032/led_sys{1,2}` | `wedge100s-i2c-daemon.c` (`apply_led_writes`) | Daemon-only write-through to CPLD kernel driver |
| `/sys/bus/i2c/devices/1-0032/{cpld_version,psuN_*}` | `wedge100s-i2c-daemon.c` (`poll_cpld`) | Daemon-only read mirror to /run/ |
| `/sys/bus/i2c/devices/{3,8}-00{48..4c}/hwmon/*/temp1_input` | `wedge100s-bmc-daemon.c` | Daemon-only TMP75 reads |
| `/sys/bus/i2c/devices/8-0033/fan*` | `wedge100s-bmc-daemon.c` | Daemon-only fan controller reads |

No Python code in `sonic_platform/` or `plugins/` references `/sys/bus/i2c`
in any live code path.  All remaining sysfs paths in those files appear only
in docstring comments.

---

## Appendix: Should pmon wait for the pollers before starting?

**Question asked 2026-03-23:** Should `wedge100s-platform-init.service` and
`pmon.service` declare `After=` dependencies on the two poller services to
ensure `/run/wedge100s/` is populated before pmon queries it?

### wedge100s-platform-init — No

`wedge100s-platform-init` is a *prerequisite of* the pollers (both timer
units declare `After=wedge100s-platform-init.service`).  Making it also
depend on the pollers would be circular.

### pmon — Not needed; graceful handling is sufficient

The pollers are **timer-activated oneshot** services.  At boot, systemd
builds one transaction for all units activated via targets
(`multi-user.target → sonic.target → pmon`).  The timers fire their
oneshot services as *separate scheduling events*, not as part of that
initial transaction.  An `After=wedge100s-i2c-poller.service` entry in
pmon's drop-in therefore has no effect: the ordering constraint only
applies if both units are in the same transaction, and the service enters
the queue later (when the timer fires), after pmon's start job is already
resolved.

The existing postinst drop-in (`wedge100s-dependency.conf`) already
establishes `After=wedge100s-platform-init.service`, and platform-init
has `Before=pmon.service`.  That is the correct and complete ordering.

**Why the race window is already covered:**

The practical window between platform-init completing and pmon's Python
code first executing is several seconds (docker container startup).  The
first i2c-daemon tick completes in ~20 ms; the first bmc-daemon tick in
~1–2 s.  In practice the daemons finish long before pmon's daemons call
into `sonic_platform/`.

The Python layer was also fixed as part of this isolation work to handle
the absent-file case gracefully:

| File | Absent-file behaviour |
|---|---|
| `eeprom.py` `get_eeprom()` | Returns `{}` without caching; retries on every call until valid data arrives |
| `sfp.py` `get_presence()` | Returns `False` (port treated as empty) |
| `sfp.py` `read_eeprom()` | Returns `None` |
| `thermal.py` `get_temperature()` | Returns `None`; thermalctld skips stale reads |
| `fan.py` `get_speed_rpm()` | Returns `None`; thermalctld skips under/over-speed checks |
| `psu.py` `get_presence()` | Returns `False` |
| `component.py` `get_firmware_version()` | Returns `"N/A"` |

The `eeprom.py` retry fix is the most important: previously `get_eeprom()`
cached an empty `{}` permanently on the first call if the daemon file was
absent, meaning `show platform syseeprom` would return nothing for the
lifetime of the process.  It now caches only on a successful non-empty
parse.

**Belt-and-suspenders alternative (not adopted):**

If a hard ordering guarantee were ever required, the most reliable
systemd-native approach for a timer-activated oneshot would be an
`ExecStartPre` wait in the drop-in:

```ini
[Service]
ExecStartPre=/bin/bash -c \
  'for f in /run/wedge100s/syseeprom /run/wedge100s/thermal_1; do \
     timeout 30 sh -c "until [ -f \"$f\" ]; do sleep 0.5; done"; \
   done'
```

This was **not added** because the graceful-handling code makes it
unnecessary, and an `ExecStartPre` loop would make boot-time failure modes
harder to diagnose (pmon hangs silently if a daemon never writes its files).
