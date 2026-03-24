# PS-04 PLAN — QSFP/SFP Subsystem

## Problem Statement

SONiC's `xcvrd` daemon requires an `Sfp` class implementing `SfpOptoeBase`
for each transceiver port. The Wedge 100S-32X has 32 QSFP28 ports. `xcvrd`
uses `get_presence()` and `read_eeprom()` to detect module insertion/removal
and to read transceiver type, vendor, and DOM data.

**Critical constraint:** The kernel `i2c_mux_pca954x` driver MUST NOT be loaded
on this platform. Loading it causes the kernel to issue probe writes to every
EEPROM address (0x50) on every virtual I2C bus during `i2c_add_adapter()`,
which corrupts QSFP EEPROM data. This was the root cause of a transceiver EEPROM
corruption incident observed in early development.

## Proposed Approach

**Primary path (normal operation):** The `wedge100s-i2c-daemon` (C program) owns
the CP2112 mux tree exclusively, reading presence and EEPROM via `/dev/hidraw0`
without creating kernel virtual buses. It writes:
- `/run/wedge100s/sfp_N_present` — `"0"` or `"1"` (N is 0-based)
- `/run/wedge100s/sfp_N_eeprom` — 256-byte EEPROM page 0 as a binary file

`Sfp.get_presence()` reads the presence cache file and checks file mtime
for staleness (> 8 s triggers a live fallback).

`Sfp.read_eeprom(offset, num_bytes)` reads from the EEPROM cache file.

**Fallback path (daemon not yet run / cache absent):** Direct `smbus2` read
of the PCA9535 presence chips at buses 36–37, addresses 0x22–0x23. The XOR-1
bit interleave (from ONL `sfpi.c`) is applied here.

**No `i2c_mux_pca954x`:** Virtual buses i2c-2 through i2c-41 do not exist in
normal operation. The `_EEPROM_PATH` sysfs fallback in `get_eeprom_path()` is
present for compatibility but will not resolve to a real file.

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py` | Sfp class |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/platform_smbus.py` | smbus2 helper |

`chassis.py` must prepend a `None` sentinel at `_sfp_list[0]` so that
`get_sfp(1)` maps to `Sfp(0)` correctly.

## Acceptance Criteria

- `Chassis()._sfp_list` has length 33 (1 sentinel + 32 Sfp objects)
- `get_presence()` correctly identifies occupied vs empty ports
- Populated ports return valid EEPROM data from `read_eeprom(0, 256)`
- `get_eeprom_path()` returns the daemon cache path when it exists
- `reset()` and `set_lpmode()` return `False` (not wired)
- `get_error_description()` returns `SFP_STATUS_UNPLUGGED` for absent ports

## Risks and Watchouts

- **Never load `i2c_mux_pca954x`:** This is the single most critical hardware
  rule. Loading this module will cause EEPROM corruption requiring PSU cycle.
- **XOR-1 interleave:** PCA9535 GPIO lines are wired with XOR-1 interleave
  (`line = port % 16 ^ 1`). Getting this wrong causes presence to be reported
  on wrong port numbers.
- **0-based vs 1-based indexing:** Port-config.ini uses 1-based SFP indices.
  `ChassisBase.get_sfp(index)` uses `_sfp_list[index]` directly. The None
  sentinel at index 0 is mandatory.
- **Staleness threshold 8 s:** The daemon fires every 3 s. 8 s allows ~2.5
  missed cycles before triggering the fallback. Do not reduce this to < 6 s.
- **Vendor string bytes 148–163 may be empty on DAC cables:** SFF-8636 vendor
  name lives at page 0 offset 148. Some DAC cables have garbled or zero vendor
  fields — see Known Gaps in IMPLEMENTATION.md and the pending investigation
  in TEST_PLAN.md before concluding hardware fault.
