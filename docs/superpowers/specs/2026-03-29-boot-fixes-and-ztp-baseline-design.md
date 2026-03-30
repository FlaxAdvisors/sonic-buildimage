# Boot Fixes and ZTP Baseline — Design Spec
Date: 2026-03-29

## Context

Three boot-time issues were fixed on running systems but never captured in source,
causing them to be wiped on every fresh install.  This spec records the correct
permanent fix location for each, plus the deploy baseline snapshot needed for ZTP
testing.

---

## Issue 1 — `crashkernel=` missing from kernel cmdline

**Symptom:**
```
kdump_mem_estimator[832]: Couldn't find systemctl
kdump-tools[835]: no crashkernel= parameter in the kernel cmdline ... failed!
```

**Root cause:** `kdump-tools` requires a pre-reserved memory region declared at
boot.  The wedge100s platform file had no `crashkernel=` arg.

**Fix location:** `installer/platforms/x86_64-accton_wedge100s_32x-r0`
Add `crashkernel=256M` to `ONIE_PLATFORM_EXTRA_CMDLINE_LINUX`.

**Status:** Fixed this session.  Takes effect on next image build + ONIE install.

**Why 256M:** Wedge 100S-32X has 4 GB RAM.  256 MB is the standard reservation
for a platform this size.  The `kdump_mem_estimator` systemctl PATH warning
is a secondary issue (early boot PATH; kdump works regardless).

---

## Issue 2 — `quiet` injected into kernel cmdline

**Symptom:** `/proc/cmdline` contains `quiet`, suppressing boot diagnostics.
This hides BCM IRQ storms, kdump events, and driver errors during bring-up.

**Root cause:** `installer/default_platform.conf:497` hardcodes `quiet` in
`DEFAULT_GRUB_CMDLINE_LINUX`.  We must not modify that file (it belongs to
upstream SONiC and affects all platforms).

**Fix location:** `installer/platforms/x86_64-accton_wedge100s_32x-r0`

Set `GRUB_CMDLINE_LINUX` before the grub function in `default_platform.conf`
runs.  The function uses `${GRUB_CMDLINE_LINUX:-"$DEFAULT_GRUB_CMDLINE_LINUX"}`
(bash `:-` default), so a pre-set value is preserved and `quiet` is never injected.

```bash
GRUB_CMDLINE_LINUX="console=tty0 console=ttyS${CONSOLE_DEV},${CONSOLE_SPEED}n8 processor.max_cstate=1 intel_idle.max_cstate=0"
export GRUB_CMDLINE_LINUX
```

**Known trade-off — CSTATES hardcoded:**
The Intel/AMD CSTATES branch in `default_platform.conf` (lines 486–494) runs
inside the grub function, after our value is frozen.  Since Wedge 100S-32X is
always Intel (Atom C2338), hardcoding `processor.max_cstate=1 intel_idle.max_cstate=0`
is correct.  If upstream `DEFAULT_GRUB_CMDLINE_LINUX` gains new args, review this
override — the affected args are ONLY those that go through `GRUB_CMDLINE_LINUX`:
console params and cstates.  All other cmdline args (`net.ifnames=0 biosdevname=0
apparmor=1 security=apparmor varlog_size=4096 usbcore.autosuspend=-1 loop= …`)
are injected separately in the grub entry template at line 600 and are unaffected.

**Status:** Fixed this session.  Takes effect on next image build + ONIE install.

---

## Issue 3 — sonic-ztp Python 3.12 SyntaxWarning (escape sequences)

**Symptom:**
```
sonic-ztp[4021]: /usr/lib/python3/dist-packages/ztp/Downloader.py:34:
    SyntaxWarning: invalid escape sequence '\c'
sonic-ztp[4021]:   \code
sonic-ztp[4021]: /usr/lib/python3/dist-packages/ztp/Logger.py:38:
    SyntaxWarning: invalid escape sequence '\c'
```

**Root cause (quilt patch):** The quilt patch
`src/sonic-ztp.patch/0001-fix-escape-Doxygen-backslash-sequences-in-ZTP-docstr.patch`
was correctly committed to the main repo in `b3ca44179`.

**Root cause (DPKG cache bypass):** sonic-ztp is a `SONIC_DPKG_DEBS` package.
The SONiC DPKG cache key is computed from `BLDENV + arch + SONIC_CACHE_RECIPE_VER
+ dep_flags`.  It does **not** include the contents of `src/*.patch/` directories.
Adding a new quilt patch does not change the cache key, so the build served the
old unpatched `.deb` from `/export/sonic/dpkg-cache/`.

**Fix:** Delete stale sonic-ztp cache entries before next build:
```bash
rm -f /export/sonic/dpkg-cache/sonic-ztp_1.0.0_all.deb-*.tgz
```

**Status:** Cache entries deleted this session.  Next build will recompile from
source, apply the quilt patch, and cache the corrected deb.

**General rule:** Whenever a quilt patch is added or updated for a `SONIC_DPKG_DEBS`
or `SONIC_MAKE_DEBS` package, manually delete the corresponding cache entry:
```bash
rm -f /export/sonic/dpkg-cache/<package>_*.deb-*.tgz
```

---

## Issue 4 — Deploy baseline for ZTP testing

`tools/deploy.py` configures the switch (breakout, portchannel, VLANs, optics)
to match `tools/topology.json`.  The resulting `config_db.json` is the baseline
that ZTP must reproduce on a fresh install.

**Procedure:**
1. Fresh install from latest `.bin`
2. Wait for `SYSTEM_READY|SYSTEM_STATE = UP`
3. Run `python3 tools/deploy.py`
4. Snapshot: `ssh admin@192.168.88.12 "sudo sonic-cfggen -d --print-data" > notes/deploy-ztp-baseline-config.json`

The snapshot in `notes/deploy-ztp-baseline-config.json` is the reference for
ZTP provisioning tests — a ZTP-provisioned switch should match this config.

**Blocker — BREAKOUT_CFG missing from port_breakout_config_db.json:**

`config interface breakout` requires `BREAKOUT_CFG` to be pre-populated in
CONFIG_DB.  On this platform, `/etc/sonic/port_breakout_config_db.json`
(sourced from `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/
port_breakout_config_db.json`) contains only `PORT` (128 broken-out entries) with
no `BREAKOUT_CFG` section.

Until this is fixed, `deploy.py` breakout task fails with:
```
[ERROR] BREAKOUT_CFG table is NOT present in CONFIG DB
Aborted!
```

Fix: add a `BREAKOUT_CFG` section to `port_breakout_config_db.json` with the
initial mode (`1x100G[40G]`) for all 32 parent ports (Ethernet0, Ethernet4, …,
Ethernet124).  The baseline snapshot captured 2026-03-29 (`notes/deploy-ztp-baseline-config.json`)
reflects pre-breakout state (system_tuning sysctl only; breakout/portchannel/vlans/optical
tasks not yet applied).

**Also:** BGP must be disabled (`sudo config bgp shutdown all && docker stop bgp`)
on fresh install before running any perf-sensitive tasks — the bgp container saturates
the control plane CPU on a switch with no BGP peers configured.

---

## Invariant: Submodule changes must be in quilt patches

**Any change inside `src/<submodule>/` is wiped by `make init` or `make distclean`.**

Before ending any session that touches a submodule:
1. Export: `git format-patch HEAD~1 --output-directory ../src/<sub>.patch/`
2. Update series: `ls ../src/<sub>.patch/*.patch | sort | xargs -n1 basename > ../src/<sub>.patch/series`
3. Commit the `.patch/` directory to the main repo

If a fix is not in a `.patch/` file tracked by the main repo, it does not persist.
