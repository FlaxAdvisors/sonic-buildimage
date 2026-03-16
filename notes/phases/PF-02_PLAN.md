# PF-02 — CPLD Driver: Plan

## Problem Statement

The Wedge 100S-32X CPLD at i2c-1/0x32 controls:
- PSU presence and power-good status (register 0x10)
- SYS LED 1 and SYS LED 2 (registers 0x3e, 0x3f)

Without a kernel driver, the only way to read/write these registers is via
userspace `i2cget`/`i2cset` subprocess calls or the `smbus2` Python library.
Both approaches have problems:

- `i2cget`/`i2cset` subprocess calls are slow (fork + exec per read) and
  fragile (shell injection risk, PATH dependencies).
- `smbus2` reads work but do not expose the registers as standard sysfs attributes,
  meaning tools like `psud` and `ledd` that expect sysfs cannot use them.
- pmon container access requires the I2C device node to be passed through, or
  a bind-mount of the sysfs path.

A proper kernel driver exposes the CPLD as sysfs attributes under
`/sys/bus/i2c/devices/1-0032/`, which is visible inside pmon without any
device passthrough.

## Proposed Approach

Write `modules/wedge100s_cpld.c`: a minimal I2C client driver that:
1. Binds to `i2c_device_id` entry `"wedge100s_cpld"` (registered via `new_device`).
2. Calls `sysfs_create_group()` in probe to expose the attributes listed below.
3. Uses `i2c_smbus_read_byte_data` / `i2c_smbus_write_byte_data` with a retry loop
   (10 attempts, 60 ms interval) — pattern from `accton_i2c_cpld.c`.
4. Uses a `mutex` to serialise concurrent sysfs reads from multiple callers.

### Sysfs Attributes

| Attribute | Direction | Register | Notes |
|-----------|-----------|----------|-------|
| `cpld_version` | RO | 0x00 / 0x01 | "major.minor" string |
| `psu1_present` | RO | 0x10 bit 0 | 1=present (active-low inversion) |
| `psu1_pgood` | RO | 0x10 bit 1 | 1=power good |
| `psu2_present` | RO | 0x10 bit 4 | 1=present (active-low inversion) |
| `psu2_pgood` | RO | 0x10 bit 5 | 1=power good |
| `led_sys1` | RW | 0x3e | hex byte: 0=off, 1=red, 2=green, 4=blue, +8=blink |
| `led_sys2` | RW | 0x3f | same encoding |

### Files to Change

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/wedge100s_cpld.c` — new file
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/Makefile` — add `obj-m := wedge100s_cpld.o`
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/accton_wedge100s_util.py` — add `modprobe wedge100s_cpld` to `kos`, add `echo wedge100s_cpld 0x32 > ...new_device` to `mknod`
- `debian/rules` — add `wedge100s-32x` to `MODULE_DIRS` for kernel module build step
- `sonic_platform/psu.py` — replace `smbus2.read_byte_data()` with sysfs reads
- `plugins/psuutil.py` — replace `i2cget` subprocess with sysfs reads
- `plugins/led_control.py` — replace `i2cset` subprocess with sysfs writes

### Reference Implementation

`platform/broadcom/sonic-platform-modules-accton/as7712-32x/modules/accton_i2c_cpld.c`
and `leds-accton_as7712_32x.c` — both are I2C client drivers for Accton CPLDs with
the same pattern.

## Acceptance Criteria

- `modinfo wedge100s_cpld` shows the module is built and installable.
- After `python3 accton_wedge100s_util.py install`:
  - `ls /sys/bus/i2c/devices/1-0032/` shows all 7 sysfs attributes.
  - `cat /sys/bus/i2c/devices/1-0032/cpld_version` returns `2.6`.
- `psu.py` reads presence/pgood without `smbus2` import.
- `led_control.py` writes `led_sys1`/`led_sys2` without `i2cset`.

## Risks and Watchpoints

**`new_device` write exit code.** `echo driver addr > .../new_device` always
exits 0 (shell echo exit code), even if the kernel rejects the write. If
`modprobe wedge100s_cpld` fails, the subsequent `new_device` write silently
fails. Always check `ls /sys/bus/i2c/devices/1-0032/` to confirm the driver bound.

**`debian/rules` `M=` path.** The module build step uses `M=$(MOD_SRC_DIR)/...`.
An incorrect `INSTALL_MOD_PATH` will cause the `.ko` to land outside the package
staging area. The `.ko` must end up at
`/lib/modules/<kernel>/extra/wedge100s_cpld.ko` on the target.

**psu1_pgood polarity.** Bit 1 of register 0x10 is active-high (1=good),
unlike the present bits which are active-low (0=present). Do not invert pgood.

**LED write range.** `store_led_sys1/2` must reject values > 0xff to avoid
writing garbage to the CPLD. Validate before writing.
