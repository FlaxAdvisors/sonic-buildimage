# Startup Cleanup Spec — 2026-03-27

Fresh build `wedge100s-260327-af4cd1462` has four actionable startup noise issues
and one upstream issue.  Each section includes root cause, affected file, and exact fix.

---

## Issue 1 — earlyprintk missing from kernel cmdline (HIGH PRIORITY)

**Symptom:** Serial console goes dark early in boot; only kernel panic messages visible.
Without earlyprintk the kernel's own ring-buffer is not flushed to ttyS0 until the serial
driver fully initialises, which is too late to catch hangs in initrd or early boot stages.

**Root cause:** `installer/platforms/x86_64-accton_wedge100s_32x-r0` sets
`ONIE_PLATFORM_EXTRA_CMDLINE_LINUX` to only `nopat intel_iommu=off noapic`.
`earlyprintk=ttyS0,57600n8` is not included, so GRUB never emits it.

**Fix — build-time (permanent):**
Add `earlyprintk=ttyS0,57600n8` to `ONIE_PLATFORM_EXTRA_CMDLINE_LINUX` in
`installer/platforms/x86_64-accton_wedge100s_32x-r0`:

```
ONIE_PLATFORM_EXTRA_CMDLINE_LINUX="nopat intel_iommu=off noapic earlyprintk=ttyS0,57600n8"
```

**Fix — live (current install, no rebuild needed):**
Patch `/host/grub/grub.cfg` on the running target to append
`earlyprintk=ttyS0,57600n8` to the kernel line, then reboot.

```bash
ssh admin@192.168.88.12 sudo sed -i \
  's/nopat intel_iommu=off noapic$/nopat intel_iommu=off noapic earlyprintk=ttyS0,57600n8/' \
  /host/grub/grub.cfg
```

Verify with:
```bash
ssh admin@192.168.88.12 grep earlyprintk /host/grub/grub.cfg
```

---

## Issue 2 — kdump fails: no crashkernel= in cmdline (LOW PRIORITY)

**Symptoms (from boot log):**
```
kdump_mem_estimator[841]: Couldn't find systemctl
kdump-tools[848]: no crashkernel= parameter in the kernel cmdline ... failed!
```

**Root cause (primary):** `crashkernel=` is absent from the kernel cmdline.
kdump-tools requires a reserved memory region that must be declared at boot.
`installer/platforms/x86_64-accton_wedge100s_32x-r0` does not include it.

**Root cause (secondary):** `kdump_mem_estimator` can't find `systemctl`.
This is a PATH issue: kdump_mem_estimator runs very early when /usr is only partially
mounted or PATH doesn't include /bin/systemctl.  This is a separate upstream packaging
issue that we can ignore until crashkernel is working.

**Fix — build-time:**
Add `crashkernel=256M` to `ONIE_PLATFORM_EXTRA_CMDLINE_LINUX`.
256 MB is the standard SONiC recommendation for a switch without large VMs.
(Reference: `installer/platforms/x86_64-nexthop_5010-r0` uses 512M for a more
memory-rich platform; 256M is appropriate for the Wedge 100S.)

After this fix, also verify `/etc/default/kdump-tools` on the target:
rc.local substitutes `__PLATFORM__` at first boot — confirm it happened correctly.

---

## Issue 3 — docker.com DNS failure during first-boot apt-get (LOW PRIORITY)

**Symptom (from boot log):**
```
rc.local[966]: Err:4 https://download.docker.com/linux/debian trixie InRelease
rc.local[966]:   Temporary failure resolving 'download.docker.com'
rc.local[966]: W: Failed to fetch ... They have been ignored, or old ones used instead.
```

**Root cause:**
`/etc/apt/sources.list.d/docker.list` is baked into the SONiC squashfs at build time
(confirmed at `fsroot-broadcom/etc/apt/sources.list.d/docker.list`):
```
deb [arch=amd64] https://download.docker.com/linux/debian trixie stable
```

During first boot, `rc.local` does `mv /etc/apt/sources.list /etc/apt/sources.list.rc-local`
before running `apt-get update`, so the main Debian sources are removed.  But `sources.list.d/`
is left untouched.  At t≈9s the network stack is up but DNS is not yet reliably resolved,
so docker.com lookup fails.

The warning is **cosmetic** — apt ignores the failed source and installs the platform .deb
from the local file repo successfully.  No functionality is lost.

**Fix option A — modify upstream `files/image_config/platform/rc.local`:**
Temporarily move docker.list alongside sources.list during the apt-get update:

```sh
# Before apt-get update (after mv sources.list):
mv /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.list.rc-local 2>/dev/null || true

# After apt-get install (in the cleanup section):
mv /etc/apt/sources.list.d/docker.list.rc-local /etc/apt/sources.list.d/docker.list 2>/dev/null || true
```

This is an upstream file — prefer sending a PR rather than forking it.

**Fix option B — remove docker.list from the SONiC target squashfs:**
`docker.list` serves no purpose on the running switch (Docker is already installed
in the image; the source is only needed at build time in the slave container).
Removing it from `sonic_debian_extension.j2` (the build script that places it) is
cleaner and eliminates the file entirely.

*Recommendation: defer to a future upstream PR; the warning is harmless.*

---

## Issue 4 — Legacy unit migration messages (LOW PRIORITY)

**Symptom (from boot log):**
```
rc.local[1032]: wedge100s postinst: disabled legacy unit wedge100s-bmc-poller.timer (if present)
rc.local[1032]: wedge100s postinst: disabled legacy unit wedge100s-bmc-poller.service (if present)
rc.local[1032]: wedge100s postinst: disabled legacy unit wedge100s-i2c-poller.timer (if present)
rc.local[1032]: wedge100s postinst: disabled legacy unit wedge100s-i2c-poller.service (if present)
```

**Root cause:**
`debian/sonic-platform-accton-wedge100s-32x.postinst` lines 36-40 unconditionally
run `systemctl disable --now $unit 2>/dev/null || true` and always echo the
"disabled legacy unit X (if present)" message, even when the unit does not exist.
These units (`wedge100s-bmc-poller.*`, `wedge100s-i2c-poller.*`) were removed when
D2/D3 replaced the timer+oneshot daemons with persistent timerfd daemons (commit
`88cf7f1b6`, `ce2e4b0e7`).  All current installs and all future installs will be
at the persistent-daemon version, so this migration code is dead.

**Fix:**
Remove the entire legacy unit migration loop (postinst lines 36-40):

```sh
# DELETE this block:
for unit in wedge100s-bmc-poller.timer wedge100s-bmc-poller.service \
            wedge100s-i2c-poller.timer wedge100s-i2c-poller.service; do
    systemctl disable --now "$unit" 2>/dev/null || true
    echo "wedge100s postinst: disabled legacy unit $unit (if present)"
done
```

Rationale: the migration window has closed.  Every image since the D2/D3 commits
installs only the persistent daemons.  There is no upgrade path from the old
timer-based daemons that would still have these units installed.

---

## Issue 5 — ZTP Python 3.12 SyntaxWarnings (LOW PRIORITY, UPSTREAM)

**Symptom:**
```
sonic-ztp[4082]: /usr/lib/python3/dist-packages/ztp/Downloader.py:34: SyntaxWarning: invalid escape sequence '\c'
sonic-ztp[4082]: /usr/lib/python3/dist-packages/ztp/Logger.py:38: SyntaxWarning: invalid escape sequence '\c'
```

**Root cause:**
Python 3.12 promoted unrecognized escape sequences from DeprecationWarning to
SyntaxWarning.  The upstream `sonic-ztp` package has raw strings with `\c`
(e.g. in docstrings).  This is in `/usr/lib/python3/dist-packages/ztp/` which
is installed by the `sonic-ztp` Debian package — not patchable from our postinst.

**Recommendation:**
Defer upstream.  If noise becomes a problem, suppress in the ZTP systemd unit:
```
Environment=PYTHONWARNINGS=ignore::SyntaxWarning
```
but do not do this yet as it may hide real warnings.

---

## Implementation Plan

### Phase 1: Patch grub.cfg on current install (no rebuild)

Fixes Issue 1 (earlyprintk) immediately after restoring SSH access:

```bash
# Patch grub.cfg
ssh admin@192.168.88.12 sudo sed -i \
  's/nopat intel_iommu=off noapic$/nopat intel_iommu=off noapic earlyprintk=ttyS0,57600n8/' \
  /host/grub/grub.cfg

# Verify
ssh admin@192.168.88.12 grep -A2 "menuentry" /host/grub/grub.cfg | grep earlyprintk
```

### Phase 2: Code changes (one .deb rebuild, no full image rebuild needed)

| Issue | File | Change |
|-------|------|--------|
| 1 earlyprintk | `installer/platforms/x86_64-accton_wedge100s_32x-r0` | Add `earlyprintk=ttyS0,57600n8` to ONIE_PLATFORM_EXTRA_CMDLINE_LINUX |
| 2 crashkernel  | `installer/platforms/x86_64-accton_wedge100s_32x-r0` | Add `crashkernel=256M` to ONIE_PLATFORM_EXTRA_CMDLINE_LINUX |
| 4 legacy units | `debian/sonic-platform-accton-wedge100s-32x.postinst` | Remove dead migration loop |

> NOTE: Issues 1 and 2 require a **full image rebuild** (they affect grub.cfg generation
> baked into the installer), NOT just a .deb rebuild.  The .deb rebuild is needed for Issue 4.
> For the current install, Issue 1 can be live-patched (Phase 1); Issue 2 requires a rebuild.

### Phase 3: Defer

- Issue 3 (docker.com warning): file upstream PR, or accept cosmetic noise
- Issue 5 (ZTP warning): upstream only

---

## Before/After Expected Boot Log

**Before** (current):
```
[  7.76] kdump_mem_estimator: Couldn't find systemctl
[  7.88] kdump-tools: no crashkernel= parameter ... failed!
[  9.41] rc.local: Ign ... docker.com ... InRelease
[ 10.17] rc.local: Err ... docker.com ... Temporary failure resolving
[ 23.19] rc.local: wedge100s postinst: disabled legacy unit wedge100s-bmc-poller.timer (if present)
[ 23.21] rc.local: wedge100s postinst: disabled legacy unit wedge100s-bmc-poller.service (if present)
[ 23.24] rc.local: wedge100s postinst: disabled legacy unit wedge100s-i2c-poller.timer (if present)
[ 23.26] rc.local: wedge100s postinst: disabled legacy unit wedge100s-i2c-poller.service (if present)
```

**After Phase 2 + full rebuild:**
- kdump will reserve memory and start (warning may still appear for systemctl PATH until upstream fix)
- docker.com fetch attempt will continue (issue 3 deferred)
- legacy unit messages gone
- earlyprintk active from kernel start
