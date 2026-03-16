# PF-03 — Platform Init: Test Plan

## What a Passing Test Looks Like

Platform init is successful when: all required kernel modules are loaded, the
CPLD device is registered, the IRQ affinity is set, the `sonic-platform` Python
package is importable, and both daemon timer units are enabled.

## Required Hardware State

- `.deb` package installed: `dpkg -l | grep sonic-platform-accton-wedge100s-32x`
  shows `ii`.
- System rebooted (or `systemctl start wedge100s-platform-init.service` run manually).

## Test Actions

### Step 1 — Platform init service state

```bash
systemctl status wedge100s-platform-init.service
```

Expected: `active (exited)`, exit code 0.

### Step 2 — Kernel modules loaded

```bash
lsmod | grep -c -E 'i2c_dev|i2c_i801|hid_cp2112|wedge100s_cpld'
```

Expected: `4`

```bash
lsmod | grep -E 'i2c_mux_pca954x|optoe|at24|gpio_pca953x'
```

Expected: no output.

### Step 3 — CPLD device registered

```bash
ls /sys/bus/i2c/devices/1-0032/cpld_version
```

Expected: file exists (exit 0).

### Step 4 — Runtime directory exists

```bash
ls -la /run/wedge100s/
```

Expected: directory exists, writable by root, contains at minimum:
`syseeprom`, `sfp_0_present` through `sfp_31_present`, `thermal_1` through `thermal_7`.
(Files populated by daemon timers; see PF-04 and PF-05 test plans.)

### Step 5 — sonic-platform wheel installed

```bash
pip3 show sonic-platform
```

Expected: output shows `Name: sonic-platform`, `Location: .../dist-packages/`.

```bash
python3 -c "from sonic_platform import platform; p = platform.Platform(); print('OK')"
```

Expected: `OK` (no ImportError).

### Step 6 — Timer units enabled and active

```bash
systemctl is-enabled wedge100s-bmc-poller.timer
systemctl is-enabled wedge100s-i2c-poller.timer
```

Expected: `enabled` for both.

```bash
systemctl is-active wedge100s-bmc-poller.timer
systemctl is-active wedge100s-i2c-poller.timer
```

Expected: `active` for both.

### Step 7 — IRQ affinity set (eth0-TxRx-0 on CPU2)

```bash
grep 'eth0-TxRx-0' /proc/interrupts | awk '{print $1}' | tr -d ':'
```

Get the IRQ number, then:

```bash
IRQ=$(grep 'eth0-TxRx-0' /proc/interrupts | awk '{print $1}' | tr -d ':')
cat /proc/irq/${IRQ}/smp_affinity_list
```

Expected: `2` (CPU2 only).

### Step 8 — pmon.sh has /run/wedge100s bind-mount

```bash
grep 'run/wedge100s' /usr/share/sonic/templates/pmon.sh || \
grep 'run/wedge100s' /usr/bin/pmon.sh 2>/dev/null || true
```

Expected: line containing `--volume /run/wedge100s:/run/wedge100s`.

## Pass/Fail Criteria

| Check | Pass | Fail |
|-------|------|------|
| platform-init service active | `active (exited)` | failed / inactive |
| 4 required modules loaded | 4 lines in lsmod | < 4 |
| mux/optoe/at24 NOT loaded | no output | any in lsmod |
| CPLD device exists | file present | missing |
| /run/wedge100s/ exists | directory present | missing |
| sonic-platform importable | `OK` | ImportError |
| both timers enabled | `enabled` | `disabled` |
| both timers active | `active` | `inactive` |
| eth0-TxRx-0 affinity | `2` | `0-3` or other |

## Mapping to Test Stage

These checks map to `tests/stage_03_platform/`. The timer state checks also
overlap with `tests/stage_10_daemon/` (planned for PF-04 and PF-05).

## State Changes and Restoration

No state changes. All steps are read-only queries. The IRQ affinity check
reads `/proc/irq/N/smp_affinity_list` without writing.
