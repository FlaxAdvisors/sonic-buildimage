# Phase R28 — Compiled BMC Polling Daemon

## Problem

`bmc.py` opens `/dev/ttyACM0`, logs into OpenBMC, sends one command, and closes the
TTY — for **every** sensor read.  With ~28 reads needed per full poll cycle:

- thermalctld: 7 × thermal ≈ 56 s (7 × ~8 s per open/login/cmd/close)
- Total cycle including fans and PSU: ~65 s

## Solution

`wedge100s-bmc-daemon` opens the TTY **once**, logs in **once**, reads ALL sensors
in the same session, writes results to `/run/wedge100s/`, then exits.

Expected timing: ~1 s login + 28 × ~0.3 s = ~9 s worst case (vs 65 s).
In practice 3-5 s because commands don't wait for per-call login overhead.

## Files Created

| File | Role |
|---|---|
| `wedge100s-32x/utils/wedge100s-bmc-daemon.c` | C one-shot daemon source |
| `wedge100s-32x/service/wedge100s-bmc-poller.service` | systemd one-shot service unit |
| `wedge100s-32x/service/wedge100s-bmc-poller.timer` | systemd timer (10 s interval) |

## Files Modified

| File | Change |
|---|---|
| `debian/rules` | Build step: `gcc -O2` from `.c`; install: `find ! -name *.c`; copy `.timer` files |
| `debian/sonic-platform-accton-wedge100s-32x.postinst` | Enable/start timer; mkdir `/run/wedge100s`; patch `pmon.sh` for volume mount |

## Output Files in `/run/wedge100s/`

All plain decimal integers (one per file):

```
thermal_1 .. thermal_7      millidegrees C (TMP75 sysfs on BMC i2c-3 and i2c-8)
fan_present                 bitmask (0 = all trays present)
fan_1_front .. fan_5_front  front-rotor RPM
fan_1_rear  .. fan_5_rear   rear-rotor RPM
psu_1_vin, psu_1_iin, psu_1_iout, psu_1_pout   raw LINEAR11 word (PSU1)
psu_2_vin, psu_2_iin, psu_2_iout, psu_2_pout   raw LINEAR11 word (PSU2)
```

## Daemon Design

- Based on ONL `platform_lib.c` TTY code, adapted with `select()`-based timeouts
  (same as `bmc.py`) instead of ONL's unreliable `usleep + read`.
- TTY: 57600 8N1, blocking I/O with VMIN=1 (ttyACM USB-CDC requires blocking mode).
- Prompt: `":~# "` — matches any OpenBMC root shell hostname.
- Null byte appended to each write (mirrors ONL `write(fd, buf, strlen+1)`).
- Login: CR → wait for prompt; handles `" login:"` and `"Password:"`.
- Session kept open for all 28+ commands; single close at end.
- On per-command timeout: silently skips writing that file; previous value preserved.
- On login failure: exits 1; timer re-invokes in 10 s.

## Build Integration

`debian/rules` changes:
- **Build**: `gcc -O2 -o utils/wedge100s-bmc-daemon utils/wedge100s-bmc-daemon.c`
- **Clean**: `rm -f utils/wedge100s-bmc-daemon`
- **Install**: `find utils/ ! -name '*.c'` (excludes C source from `/usr/bin/`)
- **Install**: copies `*.timer` alongside `*.service` to `lib/systemd/system/`

## Postinst Actions (R28)

1. `mkdir -p /run/wedge100s`
2. `systemctl enable wedge100s-bmc-poller.timer`
3. `systemctl start wedge100s-bmc-poller.timer`
4. Patch `pmon.sh`: add `--volume /run/wedge100s:/run/wedge100s:ro` after
   the ttyACM device line (idempotent; skipped if already present).
   Prepares for R29 (Python reads files instead of TTY commands).

## Timer Design

- `OnBootSec=15`: fires 15 s after boot, after `wedge100s-platform-init.service`
  completes and pmon has started.  Ensures files exist for the first thermalctld poll.
- `OnUnitActiveSec=10`: re-runs every 10 s.  thermalctld polls every 60 s so data
  is always ≤10 s stale.

## What Changes for the User (R28 only)

**Nothing yet in the Python path.** R28 establishes the file pipeline;
Python still calls `bmc.py` per command (same 65 s poll cycle).
The speedup is visible only after **R29** replaces `bmc.file_read_int()` /
`bmc.i2cget_word()` calls with `open('/run/wedge100s/...').read()`.

## Next Step: R29

Replace in `thermal.py`, `fan.py`, `psu.py`:
```python
# Old (R28 and earlier):
raw = bmc.file_read_int('/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input')
# New (R29):
raw = int(open('/run/wedge100s/thermal_1').read().strip())
```

For PSU PMBus words: read integer, pass to `_pmbus_decode_linear11()`.
Keep `bmc.send_command('set_fan_speed.sh <pct>')` for the fan write path
(no file-based control; daemon is read-only).

## Verification Steps (once built and deployed)

```bash
# Build the .deb
BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

# Install
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x*.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 sudo systemctl stop pmon
ssh admin@192.168.88.12 sudo dpkg -i sonic-platform-accton-wedge100s-32x*.deb
ssh admin@192.168.88.12 sudo systemctl start pmon

# Check timer is active
ssh admin@192.168.88.12 systemctl status wedge100s-bmc-poller.timer

# Wait 15 s then check output files
ssh admin@192.168.88.12 ls -la /run/wedge100s/
ssh admin@192.168.88.12 cat /run/wedge100s/thermal_1     # should be ~23000-40000
ssh admin@192.168.88.12 cat /run/wedge100s/fan_1_front   # should be ~7500
ssh admin@192.168.88.12 cat /run/wedge100s/fan_present   # should be 0

# Time a single daemon run
ssh admin@192.168.88.12 'time sudo /usr/bin/wedge100s-bmc-daemon'
# Expect: real ~3-9s (vs 65s for a Python full-cycle)
```
