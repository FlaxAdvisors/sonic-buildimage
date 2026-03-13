# Phase R26 — CPLD Kernel Driver Notes

## Hardware verification (2026-03-11, kernel 6.12.41+deb13-sonic-amd64)

### What was built
- `modules/wedge100s_cpld.c`: I2C driver binding to i2c-1/0x32
- `modules/Makefile`: `obj-m := wedge100s_cpld.o`
- Sysfs path: `/sys/bus/i2c/devices/1-0032/`
- Attributes: `cpld_version` (RO), `psu1_present` (RO), `psu1_pgood` (RO),
  `psu2_present` (RO), `psu2_pgood` (RO), `led_sys1` (RW), `led_sys2` (RW)

### Hardware-verified values
- `cpld_version = 2.6` (matches i2c_bus_map.json major=0x02, minor=0x06)
- `psu1_present = 1, psu1_pgood = 0` (PSU1 present, no AC in lab)
- `psu2_present = 1, psu2_pgood = 1` (PSU2 fully operational)
- `led_sys1 = 0x02` (green), `led_sys2 = 0x02` (green)
- LED write verified: `echo 0 > led_sys2` → off, `echo 2 > led_sys2` → green

### Python layer changes
- `sonic_platform/psu.py`: reads `/sys/bus/i2c/devices/1-0032/psu{N}_present|pgood`
  instead of `platform_smbus.read_byte()` — works correctly from inside pmon
- `plugins/psuutil.py`: same sysfs reads instead of `i2cget` subprocess
- `plugins/led_control.py`: writes to `led_sys1`/`led_sys2` sysfs attrs
  instead of `i2cset` subprocess

### Build fix required
`debian/rules` `modules_install` had wrong `M=` path and wrong INSTALL_MOD_PATH:
- Before: `M=$(MOD_SRC_DIR)` → modules went to separate staging dir, not packaged
- After:  `M=$(MOD_SRC_DIR)/$${mod}/modules INSTALL_MOD_PATH=debian/$(PACKAGE_PRE_NAME)-$${mod}`
  → .ko lands at `/lib/modules/6.12.41+deb13-sonic-amd64/extra/wedge100s_cpld.ko`

### Boot sequence
1. `modprobe wedge100s_cpld` (in `kos` list in util.py)
2. `echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device` (first `mknod` entry)
3. Driver binds; sysfs attributes appear at `/sys/bus/i2c/devices/1-0032/`
4. `depmod -a` in postinst ensures module is findable by modprobe

### Silent failure mode
The `echo <driver> <addr> > .../new_device` commands in device_install() silently
succeed (exit 0) even if the write fails (echo exit code, not write exit code).
Verified: if modprobe fails, new_device write fails silently, muxes still register.
