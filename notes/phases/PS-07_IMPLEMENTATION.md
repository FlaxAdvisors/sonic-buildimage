# PS-07 IMPLEMENTATION — Build & Install

## Files Changed

- `platform/broadcom/platform-modules-accton.mk` — `.deb` target definition
- `platform/broadcom/sonic-platform-modules-accton/debian/rules` — build orchestration
- `platform/broadcom/sonic-platform-modules-accton/debian/control` — package metadata
- `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`
- `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.install`
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform_setup.py`

## Package Identity

| Field | Value |
|---|---|
| Package name | `sonic-platform-accton-wedge100s-32x` |
| Version | `1.1` (from `ACCTON_WEDGE100S_32X_PLATFORM_MODULE_VERSION` in `.mk`) |
| Architecture | `amd64` |
| Depends | `linux-image-6.12.41+deb13-sonic-amd64-unsigned` |

## Build Orchestration (`debian/rules`)

`MODULE_DIRS := wedge100s-32x` — only the wedge100s-32x directory is built
(all other Accton platforms are commented out).

`override_dh_auto_build` compiles:
1. Kernel module: `$(MAKE) -C $(KERNEL_SRC)/build M=…/modules modules`
   Produces: `wedge100s_cpld.ko`
2. BMC daemon: `gcc -O2 -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c`
3. I2C daemon: `gcc -O2 -o wedge100s-i2c-daemon wedge100s-i2c-daemon.c`
4. Python wheel: `python3 sonic_platform_setup.py bdist_wheel -d …/wedge100s-32x`
   Produces: `sonic_platform-1.0-py3-none-any.whl`

`override_dh_auto_install` installs:
- Kernel module to `debian/sonic-platform-accton-wedge100s-32x/lib/modules/…/extra/`
  via `modules_install`
- Utilities (compiled binaries + Python util) to `usr/bin/`
- Service files (`*.service`, `*.timer`) to `lib/systemd/system/`

The Python wheel is NOT installed by pybuild's dh_install — it is placed in the
device directory via the `.install` file.

## .install File

```
wedge100s-32x/sonic_platform-1.0-py3-none-any.whl usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0
```

The wheel is installed to the device directory. `postinst` then runs
`pip3 install --force-reinstall` on it, which places the `sonic_platform`
package in `/usr/lib/python3/dist-packages/sonic_platform/`.

## Python Wheel (`sonic_platform_setup.py`)

- `name='sonic-platform'`, `version='1.0'`
- `packages=['sonic_platform']`
- Installs: `sonic_platform/__init__.py`, `chassis.py`, `thermal.py`, `fan.py`,
  `psu.py`, `sfp.py`, `eeprom.py`, `watchdog.py`, `platform.py`, `bmc.py`,
  `platform_smbus.py`

## postinst Key Operations

In order of execution:
1. `depmod -a` — rebuild module dependency list
2. `systemctl enable wedge100s-platform-init.service` — enables at boot
3. `systemctl start wedge100s-platform-init.service` — starts immediately
   (calls `accton_wedge100s_util.py install`)
4. Disable `sysstat` if enabled (reduces journal noise)
5. Install `pmon.service.d/wedge100s-dependency.conf` drop-in
   (`After=wedge100s-platform-init`)
6. `systemctl daemon-reload`
7. `mkdir -p /run/wedge100s`
8. Enable + start `wedge100s-bmc-poller.timer`
9. Enable + start `wedge100s-i2c-poller.timer`
10. Copy `port_breakout_config_db.json` to `/etc/sonic/` if present
11. Patch `pmon.sh` to add `--device=/dev/ttyACM*` (idempotent, guarded by `grep -q`)
12. Patch `pmon.sh` to add `--volume /run/wedge100s:/run/wedge100s:ro` (idempotent)
13. Convert Click 7 → Click 8 bash completion files in `/etc/bash_completion.d/`
14. Remove `monit_bmp` and `monit_otel` (unused containers on this platform)
15. Silence `memory_checker` routine syslog calls
16. Make chrony DHCP hook idempotent
17. Remove stopped pmon container (if `docker rm` is safe)
18. `pip3 install --force-reinstall <wheel>` on host
19. If pmon running: `docker exec pmon pip3 install --force-reinstall <wheel>`
20. If pmon running: `docker exec pmon supervisorctl restart xcvrd psud`

The postinst uses `set -e` — any exit-non-zero aborts the install unless
guarded by `|| true`.

## Kernel Modules Loaded by `accton_wedge100s_util.py install`

```
modprobe i2c_dev
modprobe i2c_i801
modprobe hid_cp2112
modprobe wedge100s_cpld
```

`i2c_mux_pca954x`, `at24`, `optoe`, and `lm75` are intentionally NOT loaded.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64):
- `dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb` completes without
  error (multiple installs verified as part of development workflow)
- `wedge100s_cpld` loaded and `/sys/bus/i2c/devices/1-0032/` created correctly
- `sonic_platform` importable from `/usr/lib/python3/dist-packages/`
- postinst ttyACM and `/run/wedge100s` patches applied successfully to `pmon.sh`
- `wedge100s-bmc-poller.timer` and `wedge100s-i2c-poller.timer` enabled and active

## Remaining Known Gaps

- The `Depends:` field in `debian/control` pins an exact kernel version
  (`linux-image-6.12.41+deb13-sonic-amd64-unsigned`). A kernel update will
  require bumping this version.
- No prerm/postrm scripts — uninstalling the deb does not disable the systemd
  services or remove `pmon.sh` patches. Manual cleanup is required.
- postinst patches `pmon.sh` in place. If upstream SONiC changes the
  `pmon.sh` template, the `NEEDLE` string may not match and the patch is
  silently skipped, leaving ttyACM unmapped.
