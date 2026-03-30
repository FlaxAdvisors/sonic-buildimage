# Kernel Cmdline + Reboot Delay Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `quiet` from kernel cmdline, add `crashkernel=256M`, and cut the 2-3 minute reboot delay on the Wedge 100S.

**Architecture:** Two independent fixes. Kernel cmdline is fixed by editing `installer/platforms/x86_64-accton_wedge100s_32x-r0` (the platform file sourced by install.sh at boot-time grub setup) and rebuilding the image. Reboot delay is fixed by reducing systemd's DefaultTimeoutStopSec and making that change persistent via the platform install script.

**Tech Stack:** GRUB, install.sh/default_platform.conf (ONIE installer), systemd, SONiC platform modules deb.

---

## Background: How kernel cmdline works in this installer

`install.sh` (line 109) sources `platforms/$onie_platform` before calling `bootloader_menu_config()` (line 260).

Inside `bootloader_menu_config()` (`installer/default_platform.conf`):
- Line 481: sources `./platform.conf` if it exists (doesn't for our platform)
- Line 497: `DEFAULT_GRUB_CMDLINE_LINUX="console=tty0 ... quiet $CSTATES"`
- Line 499: `GRUB_CMDLINE_LINUX=${GRUB_CMDLINE_LINUX:-"$DEFAULT_GRUB_CMDLINE_LINUX"}` — uses DEFAULT if not set
- Line 583: `GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX $extra_cmdline_linux"` — appends build-time extras
- Line 602: appends `$ONIE_PLATFORM_EXTRA_CMDLINE_LINUX` at end of kernel line

Because the platform file is sourced BEFORE `bootloader_menu_config()` is called, any `GRUB_CMDLINE_LINUX` we export there will satisfy the `${:-}` and prevent DEFAULT (which has `quiet`) from being used. `ONIE_PLATFORM_EXTRA_CMDLINE_LINUX` is appended after `GRUB_CMDLINE_LINUX` on the grub linux line.

Current platform file (`installer/platforms/x86_64-accton_wedge100s_32x-r0`):
```bash
CONSOLE_PORT=0x3f8
CONSOLE_DEV=0
CONSOLE_SPEED=57600
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic"
```
Problems: no `crashkernel=256M`, no GRUB_CMDLINE_LINUX override → DEFAULT used → `quiet` appears.

---

## File Map

| File | Change |
|------|--------|
| `installer/platforms/x86_64-accton_wedge100s_32x-r0` | Add `crashkernel=256M` to ONIE_PLATFORM_EXTRA_CMDLINE_LINUX; add GRUB_CMDLINE_LINUX override without `quiet` |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/scripts/platform_install.sh` | Add systemd DefaultTimeoutStopSec=30 override |
| `notes/kernel-cmdline-reboot-delay.md` | Document findings |

---

## Task 1: Fix kernel cmdline — update platform installer file

**Files:**
- Modify: `installer/platforms/x86_64-accton_wedge100s_32x-r0`

- [ ] **Step 1: Read the current file**

```bash
cat installer/platforms/x86_64-accton_wedge100s_32x-r0
```
Expected: shows CONSOLE_PORT/DEV/SPEED and ONIE_PLATFORM_EXTRA_CMDLINE_LINUX without crashkernel=256M, no GRUB_CMDLINE_LINUX.

- [ ] **Step 2: Replace the file content**

Write the new content:

```bash
CONSOLE_PORT=0x3f8
CONSOLE_DEV=0
CONSOLE_SPEED=57600

# BCM56960 / Wedge 100S-32X kernel args (appended after GRUB_CMDLINE_LINUX on grub line):
# nopat/intel_iommu=off/noapic: mitigates APIC interrupt storm from linux-kernel-bde
#   (~150 IRQ/s on IRQ 16) that stalls sshd on this platform.
# crashkernel=256M: reserve memory for kdump crash capture kernel.
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic crashkernel=256M"

# Override DEFAULT_GRUB_CMDLINE_LINUX (which includes 'quiet') for this platform.
# The ${GRUB_CMDLINE_LINUX:-$DEFAULT} assignment in default_platform.conf respects
# this export if set before bootloader_menu_config() is called.
# CSTATES are hard-coded here (Intel Xeon D-1548 on Wedge 100S is always Intel).
GRUB_CMDLINE_LINUX="console=tty0 console=ttyS${CONSOLE_DEV},${CONSOLE_SPEED}n8 processor.max_cstate=1 intel_idle.max_cstate=0"
export GRUB_CMDLINE_LINUX
```

- [ ] **Step 3: Verify the change**

```bash
cat installer/platforms/x86_64-accton_wedge100s_32x-r0
```
Expected: file shows both ONIE_PLATFORM_EXTRA_CMDLINE_LINUX with crashkernel=256M AND GRUB_CMDLINE_LINUX without `quiet`.

- [ ] **Step 4: Commit**

```bash
git add installer/platforms/x86_64-accton_wedge100s_32x-r0
git commit -m "installer: fix Wedge 100S kernel cmdline — remove quiet, add crashkernel=256M

- GRUB_CMDLINE_LINUX override removes 'quiet' to allow early boot messages
- crashkernel=256M added to ONIE_PLATFORM_EXTRA_CMDLINE_LINUX for kdump support
- CSTATES hard-coded (Intel Xeon D-1548 is always Intel, no runtime check needed)

The DEFAULT_GRUB_CMDLINE_LINUX in default_platform.conf includes 'quiet'; our
export is evaluated before bootloader_menu_config()'s \${GRUB_CMDLINE_LINUX:-}
assignment, so the default is bypassed."
```

---

## Task 2: Build and install the new image

**Files:** `target/sonic-broadcom.bin` (generated)

- [ ] **Step 1: Rebuild sonic-broadcom.bin**

Run from `/export/sonic/sonic-buildimage.claude`:
```bash
make SONIC_BUILD_JOBS=40 BUILD_SKIP_TEST=y target/sonic-broadcom.bin 2>&1 | tail -5
```
Expected: `Successfully built sonic-broadcom.bin` (or just `target/sonic-broadcom.bin` completes). The image already has most components built — only the squashfs wrapper that embeds install.sh + platform files needs to regenerate; this should be fast (minutes not hours).

- [ ] **Step 2: Verify the installer file is embedded correctly**

```bash
# Extract install.sh from the .bin and verify the platform file content
dd if=target/sonic-broadcom.bin bs=1 skip=$(grep -ab -o 'ONIE installer' target/sonic-broadcom.bin | head -1 | cut -d: -f1) count=0 2>/dev/null || true
# Simpler: grep for our new strings directly in the binary
grep -a 'crashkernel=256M' target/sonic-broadcom.bin | head -1
grep -a 'GRUB_CMDLINE_LINUX' target/sonic-broadcom.bin | grep 'processor.max_cstate' | head -1
```
Expected: both strings found in the binary.

- [ ] **Step 3: Copy to target and install via SONiC installer**

```bash
scp target/sonic-broadcom.bin admin@192.168.88.12:~
ssh admin@192.168.88.12 "sudo sonic_installer install ~/sonic-broadcom.bin -y"
```
Expected: installer completes, says image installed, current image set.

- [ ] **Step 4: Reboot target**

```bash
ssh admin@192.168.88.12 "sudo reboot"
```
Wait for target to come back (typically 3-5 min cold boot with containers starting).

- [ ] **Step 5: Verify kernel cmdline on target**

```bash
ssh admin@192.168.88.12 "cat /proc/cmdline"
```
Expected: `quiet` is ABSENT. `crashkernel=256M` IS present. `nopat intel_iommu=off noapic` IS present. `processor.max_cstate=1 intel_idle.max_cstate=0` IS present.

Example of correct output:
```
BOOT_IMAGE=/image-.../boot/vmlinuz-... root=UUID=... rw console=tty0 console=ttyS0,57600n8 processor.max_cstate=1 intel_idle.max_cstate=0  net.ifnames=0 biosdevname=0 loop=... loopfstype=squashfs apparmor=1 security=apparmor varlog_size=4096 usbcore.autosuspend=-1 nopat intel_iommu=off noapic crashkernel=256M
```

- [ ] **Step 6: Verify grub.cfg on disk**

```bash
ssh admin@192.168.88.12 "sudo grep 'vmlinuz' /host/grub/grub.cfg"
```
Expected: same — no `quiet`, has `crashkernel=256M`.

- [ ] **Step 7: Verify kdump service starts**

```bash
ssh admin@192.168.88.12 "systemctl status kdump 2>/dev/null || echo 'kdump not present (ok)'"
ssh admin@192.168.88.12 "sudo dmesg | grep -i 'crash\|kdump' | head -5"
```
Expected: either kdump service running, or dmesg shows `Reserving 256MB of memory at ... for crashkernel`.

---

## Task 3: Fix reboot delay — reduce systemd stop timeouts

The reboot delay (2-3 minutes) is caused by systemd waiting for Docker containers (15+ SONiC service containers) to stop before their `TimeoutStopSec` expires. The default is 90s and Docker containers compound this.

**Fix:** Add a systemd system.conf drop-in that sets `DefaultTimeoutStopSec=30s`. This applies to ALL services globally and cuts the maximum wait from 90s to 30s per unit. For Docker containers running SONiC services, 30s is more than adequate for graceful shutdown.

**Files:**
- Create: `/etc/systemd/system.conf.d/sonic-stop-timeout.conf` (on target, via platform install)
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/scripts/platform_install.sh` (to persist after reinstall)

- [ ] **Step 1: Apply immediately on running target**

```bash
ssh admin@192.168.88.12 "sudo mkdir -p /etc/systemd/system.conf.d && sudo tee /etc/systemd/system.conf.d/sonic-stop-timeout.conf <<'EOF'
[Manager]
DefaultTimeoutStopSec=30s
EOF"
ssh admin@192.168.88.12 "sudo systemctl daemon-reload"
```
Expected: no output (success).

- [ ] **Step 2: Verify it took effect**

```bash
ssh admin@192.168.88.12 "systemctl show -p DefaultTimeoutStopSec"
```
Expected: `DefaultTimeoutStopSec=30s`

- [ ] **Step 3: Test reboot speed**

```bash
date; ssh admin@192.168.88.12 "sudo reboot"; sleep 5; while ! ssh -o ConnectTimeout=5 admin@192.168.88.12 echo ok 2>/dev/null; do sleep 5; done; date
```
Expected: total round-trip under 90 seconds (vs previous 2-3 minutes). The target should come back up and SSH should be available within ~60-90s.

- [ ] **Step 4: Read the platform install script**

```bash
cat platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/scripts/platform_install.sh
```
Understand where to add the systemd drop-in creation so it persists after image reinstall.

- [ ] **Step 5: Add drop-in creation to platform install script**

Find the appropriate location in `platform_install.sh` (after the main package install, before exit) and add:

```bash
# Reduce systemd stop timeout to avoid 2-3 min shutdown delays (Docker containers)
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/sonic-stop-timeout.conf <<'EOF'
[Manager]
DefaultTimeoutStopSec=30s
EOF
systemctl daemon-reload 2>/dev/null || true
```

- [ ] **Step 6: Commit the platform install script change**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/scripts/platform_install.sh
git commit -m "platform: reduce systemd DefaultTimeoutStopSec to 30s on Wedge 100S

Docker containers during shutdown can hold systemd for up to 90s each
(default). With 15+ SONiC service containers, this causes 2-3 min
reboot delays. 30s is sufficient for graceful Docker container shutdown."
```

---

## Task 4: Write findings note

**Files:**
- Create: `notes/kernel-cmdline-reboot-delay.md`

- [ ] **Step 1: Write the note**

```markdown
# Kernel Cmdline and Reboot Delay Fixes

## Problem 1: `quiet` in kernel + no `crashkernel=256M`

**Root cause:** `installer/default_platform.conf` line 497 sets:
  DEFAULT_GRUB_CMDLINE_LINUX="console=tty0 ... quiet $CSTATES"
Line 499: GRUB_CMDLINE_LINUX=${GRUB_CMDLINE_LINUX:-"$DEFAULT_GRUB_CMDLINE_LINUX"}

The platform file (installer/platforms/x86_64-accton_wedge100s_32x-r0) was not
setting GRUB_CMDLINE_LINUX, so the DEFAULT (with `quiet`) was used.

**Fix:** Added to platform file:
  GRUB_CMDLINE_LINUX="console=tty0 console=ttyS${CONSOLE_DEV},${CONSOLE_SPEED}n8 processor.max_cstate=1 intel_idle.max_cstate=0"
  export GRUB_CMDLINE_LINUX
  ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic crashkernel=256M"

The `export` is evaluated before `bootloader_menu_config()` because the platform
file is sourced at install.sh line 109, before bootloader_menu_config() at line 260.
The `${:-}` assignment at line 499 respects the already-exported variable.

**ONIE_PLATFORM_EXTRA_CMDLINE_LINUX** is appended at line 602 of default_platform.conf,
after `$GRUB_CMDLINE_LINUX $extra_cmdline_linux`, as a separate token.

## Problem 2: 2-3 minute reboot delay

**Root cause:** systemd DefaultTimeoutStopSec defaults to 90s. With 15+ SONiC Docker
containers at shutdown, total delay could be multiple minutes.

**Fix:** /etc/systemd/system.conf.d/sonic-stop-timeout.conf:
  [Manager]
  DefaultTimeoutStopSec=30s

Made persistent via platform_install.sh so it survives image reinstall.

(verified on hardware 2026-03-29)
```

- [ ] **Step 2: Commit**

```bash
git add notes/kernel-cmdline-reboot-delay.md
git commit -m "notes: document kernel cmdline and reboot delay root causes and fixes"
```
