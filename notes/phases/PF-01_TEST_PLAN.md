# PF-01 — I2C Topology: Test Plan

## What a Passing Test Looks Like

The topology is "correct" when the kernel-visible I2C surface matches Phase 2
expectations **and** the CPLD is reachable and returns expected register values.
No mux-tree buses should exist (Phase 2 architecture).

## Required Hardware State

- SONiC running on the Wedge 100S-32X (kernel 6.1.x)
- Platform init complete (`wedge100s-platform-init.service` active)
- `wedge100s-i2c-daemon` has run at least once (timer fired within 30 s of boot)
- At least one QSFP module physically installed

## Test Actions

### Step 1 — Kernel module set

```bash
lsmod | grep -E 'hid_cp2112|wedge100s_cpld|i2c_i801|i2c_dev'
```

Expected: all four modules present.

```bash
lsmod | grep -E 'i2c_mux_pca954x|optoe|at24'
```

Expected: no output (none of these loaded in Phase 2).

### Step 2 — I2C bus enumeration

```bash
ls /dev/i2c-*
```

Expected: `/dev/i2c-0` and `/dev/i2c-1` only. No `/dev/i2c-2` through `/dev/i2c-41`.

```bash
i2cdetect -l
```

Expected: exactly two adapters (`i2c-0` SMBus I801, `i2c-1` CP2112 SMBus Bridge).

### Step 3 — CPLD reachability

```bash
ls /sys/bus/i2c/devices/1-0032/
```

Expected: directory exists (CPLD driver bound).

```bash
cat /sys/bus/i2c/devices/1-0032/cpld_version
```

Expected: `2.6`

### Step 4 — CPLD PSU register live read

```bash
i2cget -f -y 1 0x32 0x10
```

Expected: `0xe0` (in the lab with PSU2 with AC, PSU1 without AC).
General pass criterion: the register returns an 8-bit value without error.
Bit 4 should be 0 (PSU2 present) when PSU2 is installed.

### Step 5 — Hidraw device accessible

```bash
ls -la /dev/hidraw0
```

Expected: device exists, readable by root (daemon runs as root).

### Step 6 — Daemon cache present and valid

After at least one timer fire (wait up to 10 s if needed):

```bash
ls /run/wedge100s/syseeprom
```

Expected: file exists.

```bash
python3 -c "
import struct
with open('/run/wedge100s/syseeprom', 'rb') as f:
    header = f.read(8)
assert header == b'TlvInfo\x00', f'Bad magic: {header!r}'
print('syseeprom TlvInfo magic: OK')
"
```

Expected: `syseeprom TlvInfo magic: OK`

```bash
ls /run/wedge100s/sfp_*_present | wc -l
```

Expected: 32

```bash
cat /run/wedge100s/sfp_0_present
```

Expected: `0` or `1` (no error).

## Pass/Fail Criteria

| Check | Pass | Fail |
|-------|------|------|
| `hid_cp2112` loaded | in lsmod | not in lsmod |
| `wedge100s_cpld` loaded | in lsmod | not in lsmod |
| `i2c_mux_pca954x` NOT loaded | not in lsmod | in lsmod |
| i2c bus count | exactly 2 (`i2c-0`, `i2c-1`) | any of `i2c-2`..`i2c-41` present |
| CPLD sysfs exists | `1-0032/` directory present | missing |
| cpld_version | `2.6` | any other value |
| PSU register readable | `0x??` without I2C error | i2cget exits non-zero |
| /dev/hidraw0 exists | present | absent |
| syseeprom TlvInfo magic | first 8 bytes = `TlvInfo\x00` | wrong magic or file missing |
| sfp_*_present count | 32 files | not 32 |

## Mapping to Test Stage

These checks map to `tests/stage_02_i2c/` (if it exists) and `tests/stage_03_platform/`.
The CPLD version check and PSU presence check are also covered by tests in
`tests/stage_09_cpld/` (planned in PF-02 test plan).

## State Changes and Restoration

These tests are read-only. The `i2cget` call to register 0x10 is a SMBUS read;
it does not alter any state. No cleanup is required.
