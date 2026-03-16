# PF-03 — Platform Init: Plan

## Problem Statement

A SONiC platform needs to be in a known state before `pmon` starts. For the
Wedge 100S-32X this means:

1. Required kernel modules loaded (`i2c_dev`, `i2c_i801`, `hid_cp2112`,
   `wedge100s_cpld`).
2. CPLD I2C device registered at i2c-1/0x32 (`new_device`).
3. `sonic_platform` wheel installed for Python imports by `pmon`.
4. BCM56960 IRQ affinity set to prevent SSH blackouts under high interrupt load.
5. Runtime directory `/run/wedge100s/` created so BMC daemon and I2C daemon can
   write their cache files.
6. systemd timer units for both daemons enabled and started.
7. pmon container configured to receive bind-mounts for `/dev/ttyACM0` and
   `/run/wedge100s/`.

Without this phase, `pmon` starts with no platform support and all
`get_thermal()` / `get_fan()` / `get_psu()` calls fail immediately.

## Proposed Approach

Two components:

### A. `utils/accton_wedge100s_util.py` (runtime init)

Python script with `install` and `clean` subcommands. Called by the systemd service.

- `install`: calls `driver_install()` (modprobe sequence), `device_install()`
  (new_device writes), `_pin_bcm_irq()`, `do_sonic_platform_install()` (pip3 whl).
- `clean`: reverse sequence; refuses if pmon is running (I2C bus safety).

Key lists in the script:
```python
kos = ['modprobe i2c_dev', 'modprobe i2c_i801', 'modprobe hid_cp2112',
       'modprobe wedge100s_cpld']
mknod = ['echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device']
```

### B. `service/wedge100s-platform-init.service` (systemd integration)

`Type=oneshot`, `RemainAfterExit=yes`, `Before=pmon.service`.
- `ExecStart=/usr/bin/accton_wedge100s_util.py install`
- `ExecStop=/usr/bin/accton_wedge100s_util.py clean`

### C. `debian/sonic-platform-accton-wedge100s-32x.postinst`

Runs at `dpkg -i` time:
- `depmod` to register new `.ko` with kernel
- `systemctl enable` and `start` for:
  - `wedge100s-platform-init.service`
  - `wedge100s-bmc-poller.timer`
  - `wedge100s-i2c-poller.timer`
- `mkdir -p /run/wedge100s`
- Patch `pmon.sh` to add `--volume /run/wedge100s:/run/wedge100s:ro` and
  `--device /dev/ttyACM0`

### Files to Change

- `utils/accton_wedge100s_util.py`
- `service/wedge100s-platform-init.service`
- `debian/sonic-platform-accton-wedge100s-32x.postinst`
- `debian/rules` (install service file, binary, whl)

## Acceptance Criteria

After `dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` or reboot:

- `systemctl status wedge100s-platform-init.service` → `active (exited)`
- `lsmod | grep wedge100s_cpld` → present
- `ls /sys/bus/i2c/devices/1-0032/` → attributes present
- `ls /run/wedge100s/` → directory exists
- `pip3 show sonic-platform` → installed

## Risks and Watchpoints

**pmon start race.** `Before=pmon.service` ensures platform-init runs first.
If `pmon` is manually restarted without running `install` first, modules may
be absent. The `driver_check()` / `device_exist()` guards prevent double-init.

**`clean` while pmon is running.** `do_uninstall()` checks Docker container
state and refuses if pmon is running. This prevents the `i2c_del_adapter`
hang that occurs when the I2C bus is released while xcvrd holds a reference.

**Phase 2: no mux registration.** The `mknod` list intentionally omits the
five PCA9548 muxes and all QSFP optoe1 devices. Any developer copy-pasting
from another Accton platform (e.g., AS7712) will find mux registration entries
there — do NOT add them. The i2c daemon owns the mux tree.

**`_pin_bcm_irq()` dynamic discovery.** The function reads `/proc/interrupts`
to find `linux-kernel-bde` and `eth0-TxRx-0` IRQ numbers rather than using
hardcoded values. This is robust across kernel versions but assumes the ASIC
driver is loaded before platform-init runs.
