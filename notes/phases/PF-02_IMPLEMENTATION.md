# PF-02 — CPLD Driver: Implementation

## What Was Built

### New file

`platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/wedge100s_cpld.c`

I2C client driver, 344 lines. Module name: `wedge100s_cpld`. Binds via
`i2c_device_id` table entry; registered manually via `new_device` (no device tree
or ACPI — standard Accton pattern).

### Key design decisions

**Retry loop.** `cpld_read()` and `cpld_write()` retry `i2c_smbus_read/write_byte_data`
up to 10 times with 60 ms sleep between attempts, matching `accton_i2c_cpld.c`.
This is necessary because the CP2112 USB bridge can be temporarily busy when the
daemon is doing mux-tree I2C work via hidraw.

**Mutex per driver instance.** `struct wedge100s_cpld_data` holds a single mutex
`update_lock`. Every sysfs handler locks it around the I2C access. This prevents
interleaved reads from multiple callers (e.g., pmon `psu.py` and `led_control.py`
running concurrently).

**PSU present polarity inversion.** Bits 0 and 4 of register 0x10 are active-low
(0=present). The `show_psu1_present` and `show_psu2_present` handlers return
`!((val >> bit) & 1)` so userspace sees `1` when the PSU is installed.

**PSU pgood is active-high.** Bits 1 and 5 are not inverted — `show_psu1_pgood`
returns `(val >> PSU1_PGOOD_BIT) & 1` directly.

**LED attributes are RW.** `led_sys1` and `led_sys2` use `S_IRUGO | S_IWUSR`.
`store_led_sys1/2` calls `kstrtoul()` and rejects values > 0xff with `-EINVAL`
before writing register 0x3e / 0x3f.

**LED show format.** Returns `"0x%02x\n"` — matches what `led_control.py` expects
when reading back the LED state. `psu.py` uses the RO integer attributes directly.

### Sysfs attributes exposed

```
/sys/bus/i2c/devices/1-0032/cpld_version   RO   "2.6\n"
/sys/bus/i2c/devices/1-0032/psu1_present   RO   "1\n" or "0\n"
/sys/bus/i2c/devices/1-0032/psu1_pgood     RO   "1\n" or "0\n"
/sys/bus/i2c/devices/1-0032/psu2_present   RO   "1\n" or "0\n"
/sys/bus/i2c/devices/1-0032/psu2_pgood     RO   "1\n" or "0\n"
/sys/bus/i2c/devices/1-0032/led_sys1       RW   "0x02\n" (example: green)
/sys/bus/i2c/devices/1-0032/led_sys2       RW   "0x02\n" (example: green)
```

### Python layer changes

`sonic_platform/psu.py`:
- Reads `/sys/bus/i2c/devices/1-0032/psu{N}_present` and `psu{N}_pgood` instead
  of calling `platform_smbus.read_byte()`. Works correctly from inside pmon
  because `/sys` is bind-mounted into the container.

`plugins/psuutil.py`:
- Same sysfs reads, replacing `i2cget` subprocess calls.

`plugins/led_control.py`:
- Writes integer to `led_sys1`/`led_sys2` sysfs attributes instead of
  `i2cset -f -y 1 0x32 0x3e/0x3f` subprocess calls.

### Build integration

`modules/Makefile`: `obj-m := wedge100s_cpld.o`

`debian/rules` fix required: the original `M=$(MOD_SRC_DIR)` path caused the
compiled `.ko` to land outside the packaging staging directory. Fixed to:
`M=$(MOD_SRC_DIR)/$${mod}/modules INSTALL_MOD_PATH=debian/$(PACKAGE_PRE_NAME)-$${mod}`

The `.ko` installs to `/lib/modules/<kernel>/extra/wedge100s_cpld.ko`.

### Boot sequence

1. `depmod -a` (in postinst — ensures module is findable by modprobe)
2. `modprobe wedge100s_cpld` (in `kos` list in `accton_wedge100s_util.py`)
3. `echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device`
   (first entry in `mknod` list)
4. Kernel binds driver; sysfs attributes appear under `1-0032/`

## Hardware-Verified Facts

- `cpld_version` = `2.6` (verified on hardware 2026-03-11)
- `psu1_present` = `1`, `psu1_pgood` = `0` (PSU1 present, no AC in lab)
- `psu2_present` = `1`, `psu2_pgood` = `1` (PSU2 fully operational)
- `led_sys1` = `0x02` (green on init), `led_sys2` = `0x02` (green on init)
- LED write verified on hardware 2026-03-11: `echo 0 > led_sys2` → off,
  `echo 2 > led_sys2` → green (observed on hardware front panel)

## Remaining Known Gaps

- `show_led_sys1/2` returns hex string (`"0x02\n"`). If `chassis.py`
  `set_status_led()` (Phase PW-01) compares the readback against a color constant
  integer, it must parse the hex format.
- No interrupt support — all reads are polling. Acceptable for CPLD frequency.
- The silent failure mode of `echo driver addr > .../new_device` (exit 0 even
  when the write fails) is a known issue with the platform init approach and is
  not specific to this driver.
