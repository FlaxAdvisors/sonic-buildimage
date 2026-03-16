# PS-07 PLAN — Build & Install

## Problem Statement

The platform code (kernel module, Python APIs, daemons, service files) must be
packaged as a single Debian `.deb` file that can be:
1. Embedded in the SONiC ONIE installer image (lazy-installed at ONIE time)
2. Re-installed live on a running SONiC system for rapid iteration (`dpkg -i`)

The `.deb` install must not hang the system, corrupt QSFP EEPROMs, or leave
the `sonic_platform` Python package in a stale state.

## Proposed Approach

**Package name:** `sonic-platform-accton-wedge100s-32x_1.1_amd64.deb`

**Build pipeline:**
1. `platform/broadcom/platform-modules-accton.mk` defines the `.deb` target and
   dependencies (linux-headers, pddf-platform-module)
2. `platform/broadcom/sonic-platform-modules-accton/debian/rules` orchestrates:
   - Kernel module build via `$(MAKE) -C $(KERNEL_SRC)/build M=…/modules modules`
   - Daemon binary build: `gcc -O2 wedge100s-bmc-daemon.c` and
     `gcc -O2 wedge100s-i2c-daemon.c`
   - Python wheel build: `python3 sonic_platform_setup.py bdist_wheel`
3. `debian/control` declares `Depends: linux-image-6.12.41+deb13-sonic-amd64-unsigned`
4. `debian/sonic-platform-accton-wedge100s-32x.install` installs the wheel to
   `usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/`
5. `debian/sonic-platform-accton-wedge100s-32x.postinst` runs post-install steps

**Python wheel:** Built from `sonic_platform_setup.py` (not `setup.py`).
Installed via `pip3 install --force-reinstall` in postinst, not via pybuild's
dh_install. This ensures the wheel lands in
`/usr/lib/python3/dist-packages/sonic_platform/`.

**Lazy install:** `platform/broadcom/one-image.mk` lists the `.deb` in
`_LAZY_INSTALLS`. During ONIE install, `install.sh` detects the platform string
`x86_64-accton_wedge100s_32x-r0` and extracts the correct `.deb`.

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/platform-modules-accton.mk` | `.deb` target definition |
| `platform/broadcom/sonic-platform-modules-accton/debian/rules` | Build orchestration |
| `platform/broadcom/sonic-platform-modules-accton/debian/control` | Package metadata |
| `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` | Post-install script |
| `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.install` | File installation map |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform_setup.py` | Python wheel setup |

## Acceptance Criteria

- `dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` exits 0
- `dpkg -l | grep wedge100s` shows `ii  sonic-platform-accton-wedge100s-32x  1.1`
- `python3 -c "from sonic_platform.chassis import Chassis; print('OK')"` succeeds
- `/usr/lib/python3/dist-packages/sonic_platform/` directory exists with all modules
- `wedge100s_cpld.ko` listed in `lsmod` after install
- systemd services enabled: `wedge100s-platform-init`, `wedge100s-bmc-poller.timer`,
  `wedge100s-i2c-poller.timer`

## Risks and Watchouts

- **postinst `set -e`:** The script starts with `set -e`. Any command that exits
  non-zero aborts the install. All optional operations (sysstat, monit, pmon
  patching) must use `|| true` or explicit error guards.
- **docker commands in postinst:** `docker exec pmon ...` fails if pmon is not
  running. Always guard with `[ "$PMON_STATUS" = "running" ]` checks.
- **pmon.sh patching:** The ttyACM and `/run/wedge100s` volume patches are
  idempotent (guarded by `grep -q`). Running postinst twice is safe.
- **Wheel force-reinstall:** `pip3 install --force-reinstall` is used so that
  a deb upgrade replaces the old wheel without needing to restart pmon.
- **QSFP safety:** The postinst does NOT load `i2c_mux_pca954x`. The kernel
  module list in `accton_wedge100s_util.py` (`mknod` array) explicitly avoids it.
