# PS-07 TEST PLAN — Build & Install

## Test Stage Mapping

`tests/stage_03_platform/`

## Required Hardware State

- Switch running SONiC (hare-lorax build on Wedge 100S-32X)
- `.deb` file available at `~/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`
  (either SCP'd from build host or previously installed)
- No pmon container forceful removal expected — tests must work with pmon running

## Step-by-Step Test Actions

### 1. Verify package is installed

```bash
dpkg -l | grep wedge100s
```

Expected output contains:
```
ii  sonic-platform-accton-wedge100s-32x  1.1  amd64  ...
```

Pass: line starts with `ii` (installed, no errors).
Fail: `un` (not installed), `rc` (removed but config remains), or no output.

### 2. Verify Python package import

```bash
ssh admin@192.168.88.12 \
  "python3 -c 'from sonic_platform.chassis import Chassis; print(\"OK\")'"
```

Expected: prints `OK`, exit code 0.

### 3. Verify wheel installation location

```bash
ssh admin@192.168.88.12 \
  "ls /usr/lib/python3/dist-packages/sonic_platform/"
```

Expected files present:
- `__init__.py`
- `chassis.py`
- `thermal.py`
- `fan.py`
- `psu.py`
- `sfp.py`
- `eeprom.py`
- `bmc.py`
- `watchdog.py`
- `platform_smbus.py`

### 4. Verify wheel in device directory

```bash
ssh admin@192.168.88.12 \
  "ls /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/sonic_platform-1.0-py3-none-any.whl"
```

Expected: file exists, exit code 0.

### 5. Verify kernel module loaded

```bash
ssh admin@192.168.88.12 "lsmod | grep wedge100s_cpld"
```

Expected: one line starting with `wedge100s_cpld`.

```bash
ssh admin@192.168.88.12 "lsmod | grep hid_cp2112"
```

Expected: one line starting with `hid_cp2112`.

### 6. Verify CPLD sysfs device registered

```bash
ssh admin@192.168.88.12 "ls /sys/bus/i2c/devices/1-0032/"
```

Expected: directory exists and contains attributes including `psu1_present`,
`led_sys1`, `led_sys2`.

### 7. Verify systemd services enabled

```bash
ssh admin@192.168.88.12 \
  "systemctl is-enabled wedge100s-platform-init.service \
   wedge100s-bmc-poller.timer \
   wedge100s-i2c-poller.timer"
```

Expected: three lines each containing `enabled`.

### 8. Verify daemon cache directory and timer activity

```bash
ssh admin@192.168.88.12 "ls -la /run/wedge100s/ | head -5"
```

Expected: directory exists, contains `thermal_1` through `thermal_7`,
`fan_present`, `fan_1_front`, `sfp_0_present`, `syseeprom`.

### 9. Verify postinst pmon.sh patches applied

```bash
ssh admin@192.168.88.12 "grep -c 'ttyACM' /usr/bin/pmon.sh"
```

Expected: `1` (at least one line containing `ttyACM`).

```bash
ssh admin@192.168.88.12 "grep -c 'run/wedge100s' /usr/bin/pmon.sh"
```

Expected: `1`.

### 10. Verify no banned kernel module

```bash
ssh admin@192.168.88.12 "lsmod | grep pca954"
```

Expected: empty output (exit code 1 from grep). If `i2c_mux_pca954x` appears,
FAIL — EEPROM corruption risk.

### 11. Verify postinst completed without error in syslog

```bash
ssh admin@192.168.88.12 \
  "sudo journalctl -b --no-pager | grep 'wedge100s postinst' | tail -5"
```

Expected: lines ending with success messages (no `ERROR:` or `FAILED:`).

### 12. Live re-install idempotency test (optional, use with pmon running)

```bash
ssh admin@192.168.88.12 "sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb"
```

Expected: exit code 0. No new `ERROR` in syslog. `sonic_platform` still importable.

## Pass/Fail Criteria

| Criterion | Pass | Fail |
|---|---|---|
| dpkg -l status | `ii` | `un`, `rc`, absent |
| Python import | Exit 0, prints OK | Exception or exit non-0 |
| `/usr/lib/python3/dist-packages/sonic_platform/` | All .py files present | Missing files |
| `wedge100s_cpld` in lsmod | Present | Absent |
| `hid_cp2112` in lsmod | Present | Absent |
| `/sys/bus/i2c/devices/1-0032/` | Exists with attributes | Missing |
| systemd services enabled | All 3 `enabled` | Any `disabled` or `masked` |
| `/run/wedge100s/` populated | ≥ 5 daemon files | Empty or absent |
| `pmon.sh` patched (ttyACM) | grep count ≥ 1 | 0 |
| `pmon.sh` patched (run/wedge100s) | grep count ≥ 1 | 0 |
| `pca954` NOT in lsmod | Empty grep | Present |

## State Changes and Restoration

Steps 1–11 are read-only inspection. Step 12 (re-install) modifies:
- `/usr/lib/python3/dist-packages/sonic_platform/` (overwrites with same content)
- `/usr/bin/pmon.sh` (patch is idempotent — no second patch applied)
- May restart `xcvrd` and `psud` inside pmon container

The re-install does not change hardware state (no register writes). After the
test, the system is in the same functional state as before.
