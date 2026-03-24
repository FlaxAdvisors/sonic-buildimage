# CPLD Subsystem

## Hardware

- Single CPLD at host `i2c-1` / `0x32`
- I2C bus: CP2112 USB HID bridge exposed by `hid_cp2112` as `i2c-1`
- CPLD version observed: major `0x02`, minor `0x06`
- Board ID register `0x02`: value `0x65`

**CPLD register map** (from ONL `ledi.c` / `psui.c`, verified 2026-02-25):

| Register | Name | Access | Description |
|---|---|---|---|
| `0x00` | VERSION_MAJOR | RO | CPLD major version |
| `0x01` | VERSION_MINOR | RO | CPLD minor version |
| `0x02` | BOARD_ID | RO | Board ID (`0x65`) |
| `0x10` | PSU_STATUS | RO | PSU presence and power-good bits |
| `0x3e` | LED_SYS1 | RW | SYS1 LED (0=off, 1=red, 2=green, 4=blue, +8=blink) |
| `0x3f` | LED_SYS2 | RW | SYS2 LED (same encoding) |

**PSU status register `0x10` bit layout:**

| Bit | Signal | Polarity | Sysfs attribute |
|---|---|---|---|
| 0 | PSU1_PRESENT | 0 = present | `psu1_present` (driver inverts: 1 = present) |
| 1 | PSU1_PGOOD | 1 = good | `psu1_pgood` |
| 4 | PSU2_PRESENT | 0 = present | `psu2_present` (driver inverts) |
| 5 | PSU2_PGOOD | 1 = good | `psu2_pgood` |

Live observed value `0xe0`: bits 7 and 6 are set (unrelated to PSU/LED); bit 0 set
= PSU1 absent; bit 4 clear = PSU2 present.

## Driver / Daemon

- **Kernel module:** `wedge100s_cpld` (Phase R26)
- **Source:** `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/modules/wedge100s_cpld.c`
- **Driver name:** `wedge100s_cpld` (used for `new_device` registration)
- **Registration:** `echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device`
  (executed by `accton_wedge100s_util.py` at boot)
- **I2C functionality required:** `I2C_FUNC_SMBUS_BYTE_DATA`
- **Retry policy:** up to 10 retries at 60 ms intervals for all register reads/writes
- **Locking:** per-device `struct mutex update_lock` serialises all register accesses
  within the driver

**Sysfs attributes exposed at `/sys/bus/i2c/devices/1-0032/`:**

| Attribute | R/W | Format |
|---|---|---|
| `cpld_version` | RO | `"major.minor\n"` e.g. `"2.6\n"` |
| `psu1_present` | RO | `"0\n"` or `"1\n"` |
| `psu1_pgood` | RO | `"0\n"` or `"1\n"` |
| `psu2_present` | RO | `"0\n"` or `"1\n"` |
| `psu2_pgood` | RO | `"0\n"` or `"1\n"` |
| `led_sys1` | RW | hex: `"0x02\n"` on read; decimal or hex string on write |
| `led_sys2` | RW | same encoding |

All attributes use `i2c_smbus_read_byte_data` / `i2c_smbus_write_byte_data`.
`led_sys1` and `led_sys2` accept any value 0–255 via `kstrtoul(buf, 0, &val)`.

## Python API

Direct CPLD sysfs reads are performed by `psu.py`, `chassis.py`, and `led_control.py`.
There is no dedicated Python CPLD class; the driver is accessed via file I/O.

**Read pattern (used in `psu.py`):**
```python
_CPLD_SYSFS = '/sys/bus/i2c/devices/1-0032'

def _read_cpld_attr(name):
    with open('{}/{}'.format(_CPLD_SYSFS, name)) as f:
        return int(f.read().strip(), 0)
```

**Write pattern (used in `chassis.py` and `led_control.py`):**
```python
with open('{}/led_sys1'.format(_CPLD_SYSFS), 'w') as f:
    f.write(str(val))   # val is integer 0x00–0xff
```

`platform_smbus.read_byte(1, 0x32, reg)` is available as a low-level alternative
for direct register reads without the kernel driver (uses `force=True` to bypass
driver binding).

## Pass Criteria

- `ls /sys/bus/i2c/devices/1-0032/` lists all 7 sysfs attributes
- `cat /sys/bus/i2c/devices/1-0032/cpld_version` returns `"2.6"` (or similar non-zero)
- `cat /sys/bus/i2c/devices/1-0032/psu2_present` returns `"1"` when PSU2 is inserted
- `cat /sys/bus/i2c/devices/1-0032/psu2_pgood` returns `"1"` when PSU2 has power
- `echo 2 > /sys/bus/i2c/devices/1-0032/led_sys1` succeeds (return code 0) and
  `cat /sys/bus/i2c/devices/1-0032/led_sys1` returns `"0x02"`
- `dmesg | grep wedge100s` shows `"wedge100s CPLD at 0x32"` after module load

## Known Gaps

- CPLD register `0x02` (Board ID) is not exposed as a sysfs attribute; it is
  documented in `i2c_topology.json` but absent from the driver's attribute group.
- No interrupt support; CPLD does not generate IRQs to the host CPU (PSU hotplug
  events are detected only by polling `psu1_present` / `psu2_present`).
- The driver exposes only the subset of CPLD registers used by known subsystems
  (PSU status, 2 LEDs). Other CPLD registers (if any) are not documented or accessible
  via sysfs.
- `set_status_led()` for PSU objects calls the CPLD only indirectly (through chassis
  LED logic); the CPLD has no dedicated PSU-status LED output.
- CPLD version attributes are read-only; there is no firmware update mechanism in the driver.
