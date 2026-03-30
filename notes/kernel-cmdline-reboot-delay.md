# Kernel Cmdline and Reboot Delay Fixes

## Problem 1: `quiet` in kernel + no `crashkernel=256M`

### Symptoms
After fresh SONiC install on Wedge 100S-32X:
- `/proc/cmdline` contains `quiet` (suppresses early boot messages)
- `crashkernel=256M` absent → kdump reservation never made → kdump service fails

### Root Cause
`installer/default_platform.conf` `bootloader_menu_config()`:
```
line 497: DEFAULT_GRUB_CMDLINE_LINUX="console=tty0 ... quiet $CSTATES"
line 499: GRUB_CMDLINE_LINUX=${GRUB_CMDLINE_LINUX:-"$DEFAULT_GRUB_CMDLINE_LINUX"}
```
The `${:-}` uses DEFAULT if `GRUB_CMDLINE_LINUX` is unset. The platform file
(`installer/platforms/x86_64-accton_wedge100s_32x-r0`) was not setting
`GRUB_CMDLINE_LINUX`, so the DEFAULT (with `quiet`) was always used.

`ONIE_PLATFORM_EXTRA_CMDLINE_LINUX` (appended at line 602) was also missing
`crashkernel=256M`.

### Fix
Updated `installer/platforms/x86_64-accton_wedge100s_32x-r0`:
```bash
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic crashkernel=256M"

# Override DEFAULT_GRUB_CMDLINE_LINUX (which includes 'quiet')
GRUB_CMDLINE_LINUX="console=tty0 console=ttyS${CONSOLE_DEV},${CONSOLE_SPEED}n8 processor.max_cstate=1 intel_idle.max_cstate=0"
export GRUB_CMDLINE_LINUX
```

The `export` is evaluated before `bootloader_menu_config()` because the platform
file is sourced at `install.sh` line 109, before `bootloader_menu_config()` at
line 260. The `${:-}` at line 499 respects the already-exported variable.

`ONIE_PLATFORM_EXTRA_CMDLINE_LINUX` is appended at line 602 after
`$GRUB_CMDLINE_LINUX $extra_cmdline_linux` as a separate token.

CSTATES are hard-coded (Intel Xeon D-1548 on Wedge 100S is always Intel; no
runtime `/proc/cpuinfo` check needed for a single-platform file).

Committed: `8fe59134f` — "installer: fix Wedge 100S kernel cmdline — remove quiet, add crashkernel=256M"

---

## Problem 2: 2-3 minute reboot delay

### Symptoms
`sudo reboot` hangs for 2-3 minutes during the systemd stop phase.

### Root Cause
`/var/log` is a loop-mounted ext4 filesystem:
- Device: `/dev/loop1`
- Backing file: `/host/disk-img/var-log.ext4`
- Mount: auto-created by SONiC initramfs (no unit file in /etc/systemd or /lib/systemd)

During shutdown, systemd tries to unmount `var-log.mount` but the following
processes hold `/var/log` open:
- `auditd` (PID ~641, host process writing audit logs)
- `rsyslogd` (host syslog daemon)
- `orchagent` (appears on host namespace because the `swss` Docker container
  bind-mounts `/var/log` from the host)

`umount` blocks until these processes release their file descriptors or the
`DefaultTimeoutStopSec` (90s) fires. With multiple such units, total delay
compounds to 2-3 minutes.

Changing `/var/log` to a real partition is NOT the fix: the loop device is
mounted in the SONiC initramfs (`/host/disk-img/var-log.ext4`) and changing it
would require rebuilding the kernel/initramfs. The processes holding `/var/log`
open would still cause the same problem on a real partition.

### Fix

**A: `wedge100s-pre-shutdown.service`** — runs `ExecStop` at shutdown
**before** `var-log.mount` is unmounted, after Docker/rsyslog/auditd stop:

```ini
[Unit]
After=var-log.mount
Before=docker.service rsyslog.service auditd.service
```
- `After=var-log.mount`: ExecStop fires before var-log.mount stops (reversed stop ordering)
- `Before=docker.service rsyslog.service auditd.service`: those stop first

`ExecStop` script (`wedge100s-pre-shutdown.sh`):
```bash
fuser -k -TERM /var/log 2>/dev/null || true
sleep 2
fuser -k -KILL /var/log 2>/dev/null || true
umount /var/log 2>/dev/null || umount -l /var/log 2>/dev/null || true
```

**B: `var-log.mount.d/before-docker.conf`** drop-in — belt-and-suspenders,
ensures Docker is fully stopped before systemd even attempts the unmount
independently of the pre-shutdown service.

Both installed via `sonic-platform-accton-wedge100s-32x` postinst. Service file
and script ship in the platform deb (picked up automatically by existing
`debian/rules` glob rules).

Committed: `6378a1024` — "platform: add wedge100s-pre-shutdown service for clean /var/log loop unmount"
Fixed:     `8cae96283` — "fix(postinst): daemon-reload before systemctl enable in pre-shutdown block"

(verified on hardware 2026-03-30)
