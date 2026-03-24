# PF-04 — BMC Daemon: Plan

## Problem Statement

Seven TMP75 thermal sensors, five fan trays, and two PSUs are managed by the
OpenBMC running on the ASPEED AST2400. They are not visible to the host kernel
via any I2C bus — the only host-accessible path is via `/dev/ttyACM0` (USB CDC
ACM, 57600 baud).

The existing Python approach (`bmc.py`) opens the TTY, logs in, sends a single
command, closes the TTY — once per sensor read. With 28 reads needed per full
poll cycle:

- 7 thermal reads × ~8 s per read ≈ 56 s
- Full cycle (thermal + fan + PSU) ≈ 65 s

`thermalctld` polls every 60 s. The sensor cycle takes longer than the polling
interval, which causes `thermalctld` to skip poll cycles and stall fan control.

## Proposed Approach

Write `utils/wedge100s-bmc-daemon.c`: a compiled C binary that:
1. Opens `/dev/ttyACM0` **once**.
2. Logs in **once** (handles both fresh login and already-logged-in states).
3. Sends all 28+ sensor commands in sequence within the same TTY session.
4. Writes results as plain decimal integers to `/run/wedge100s/` (one file per value).
5. Closes the TTY and exits.

Invoked as a one-shot binary by a systemd timer every 10 seconds.

### Sensor commands

| Group | Count | Source on BMC |
|-------|-------|---------------|
| Thermal | 7 | `cat /sys/bus/i2c/devices/3-004{8,9,a,b,c}/hwmon/*/temp1_input` (bus 3) + buses 8-0048/8-0049 |
| Fan presence | 1 | `cat /sys/bus/i2c/devices/8-0033/fantray_present` |
| Fan RPM | 10 | `cat /sys/bus/i2c/devices/8-0033/fan{1..10}_input` |
| PSU PMBus | 8 | `i2cset` mux select + `i2cget -w` × 4 registers × 2 PSUs |

### Output files

All in `/run/wedge100s/`, plain decimal integer per file:
`thermal_1..7`, `fan_present`, `fan_{1..5}_front`, `fan_{1..5}_rear`,
`psu_{1,2}_{vin,iin,iout,pout}`.

### Service files

`service/wedge100s-bmc-poller.service` — Type=oneshot, calls `/usr/bin/wedge100s-bmc-daemon`.
`service/wedge100s-bmc-poller.timer` — `OnBootSec=15`, `OnUnitActiveSec=10`.

### Files to Change

- `utils/wedge100s-bmc-daemon.c` — new file
- `service/wedge100s-bmc-poller.service` — new file
- `service/wedge100s-bmc-poller.timer` — new file
- `debian/rules` — gcc build step; install binary and timer files
- `debian/sonic-platform-accton-wedge100s-32x.postinst` — enable/start timer;
  mkdir `/run/wedge100s`; patch pmon.sh for volume mount
- `sonic_platform/thermal.py` — replace `bmc.file_read_int()` with file reads
- `sonic_platform/fan.py` — replace BMC calls with file reads
- `sonic_platform/psu.py` — replace `bmc.i2cget_word()` with file reads

## Acceptance Criteria

- Timer active: `systemctl is-active wedge100s-bmc-poller.timer` = `active`
- After 30 s from boot, `/run/wedge100s/thermal_1` exists and is in range
  `[15000, 80000]` (millidegrees C).
- `fan_1_front` in range `[4000, 16000]` (RPM).
- `fan_present` = `0` (all 5 trays installed in lab).
- Full daemon run completes in < 10 s (`time /usr/bin/wedge100s-bmc-daemon`).

## Risks and Watchpoints

**TTY exclusive access.** Only one process should hold `/dev/ttyACM0` at a time.
If `bmc.py` is still running (e.g., from a Python debug session) while the daemon
fires, the TTY open will fail and the daemon exits with code 1. The timer
re-invokes in 10 s. No fcntl lock is implemented — cross-process coordination
depends on the 10 s interval being sufficient.

**LOGIN_RETRY limit.** If the BMC is busy (reboot, firmware update), login
may time out. The daemon exits 1; files retain their previous values. This is
acceptable because `thermalctld` uses the stale values until the next successful
poll.

**PSU PMBus mux is single-byte write (no register prefix).** The PCA9546 on
BMC bus 7 uses a single-byte write to select channels: `i2cset -f -y 7 0x70 0x02`
(channel 2 for PSU1). This is NOT a standard SMBus byte-data write with a
register address first. Using the wrong `i2cset` form silently fails.

**pout = LINEAR11 raw word.** The daemon writes the raw PMBus LINEAR11 16-bit
word to `psu_N_pout`, etc. Python `psu.py` must decode it with `_pmbus_decode_linear11()`.
The file does NOT contain a milliwatt value.

**OnBootSec=15.** Must be longer than the time for `wedge100s-platform-init.service`
to complete (typically 3–5 s) plus pmon start time. 15 s provides enough margin.
