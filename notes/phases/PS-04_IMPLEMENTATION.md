# PS-04 IMPLEMENTATION — QSFP/SFP Subsystem

## Files Changed

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`
  (new file, extends `SfpOptoeBase`)
- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/platform_smbus.py`
  (pre-existing, smbus2 helper used by fallback path)

## Port-to-Bus Map

`_SFP_BUS_MAP` (0-based port index → i2c bus number, from ONL `sfpi.c`
`sfp_bus_index[]`):

```
Ports  0- 7: buses  3, 2, 5, 4, 7, 6, 9, 8
Ports  8-15: buses 11,10,13,12,15,14,17,16
Ports 16-23: buses 19,18,21,20,23,22,25,24
Ports 24-31: buses 27,26,29,28,31,30,33,32
```

Odd/even interleave within each group of 2: port N → bus (N*2+3 - N%2*2) in
simplified form. This matches the physical QSFP cage wiring on the Wedge 100S.

## Daemon Cache Paths

```
/run/wedge100s/sfp_N_present    (N = 0-based port index, 0–31)
/run/wedge100s/sfp_N_eeprom     (N = 0-based port index, 0–31)
```

- Presence files: text, `"0"` or `"1"` followed by newline
- EEPROM files: binary, 256 bytes (page 0 only)

## Presence Detection Logic (`get_presence()`)

1. `os.stat()` the presence cache file
2. If mtime is within `_PRESENCE_MAX_AGE_S` (8 s): read file, return `True`
   if content is `"1"`
3. If cache is stale or absent: fall through to smbus2 fallback

Fallback:
- group = port // 16 → selects PCA9535 chip (bus 36 addr 0x22, or bus 37 addr 0x23)
- line = (port % 16) ^ 1 → XOR-1 interleave (ONL `sfpi.c` bit swap)
- reg = line // 8 → INPUT0 (reg 0) or INPUT1 (reg 1)
- bit = line % 8
- `not bool((byte >> bit) & 1)` → active-low: bit=0 means present

## EEPROM Read Logic (`read_eeprom()`)

1. Open `/run/wedge100s/sfp_N_eeprom`, seek to `offset`, read `num_bytes`
2. If successful and `len(data) == num_bytes`: return `bytearray`
3. Cache miss: check presence cache; return `None` if known absent
4. Fallback: acquire `_eeprom_bus_lock` (RLock), call `SfpOptoeBase.read_eeprom()`
   which reads from `get_eeprom_path()` (sysfs path — will fail if no optoe
   driver, returning `None`)

The bus lock is an `RLock` to handle the re-entrant call pattern in
`SfpOptoeBase` where `read_eeprom()` is called from `get_optoe_current_page()`
which is called from `read_eeprom()` when offset ≥ 128.

## Unsupported Operations

- `reset()` → `False` (RESET pin on mux board, not accessible from host)
- `set_lpmode()` → `False` (LP_MODE pin on mux board, not accessible)
- `get_reset_status()` → `False`
- `get_lpmode()` → `False`

Per ONL `sfpi.c`: "the QSFP LP_MODE and RESET pins are wired through the mux
board and cannot be driven from the host CPU."

## Key Decisions

**`get_eeprom_path()` returns daemon cache path when present.** `xcvrd` calls
this method to determine where to read EEPROM. Returning the daemon cache path
keeps all EEPROM access via the cache even if xcvrd tries to bypass
`read_eeprom()` and read the file directly.

**`get_status()` delegates to `get_presence()`.** A QSFP28 is "OK" if and only
if it is physically present. No additional health register is checked.

**`NUM_SFPS = 32`** — all 32 ports are QSFP28. There are no SFP+ or SFP28
ports.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64, confirmed during
Phase R29 daemon work):
- Daemon files `/run/wedge100s/sfp_N_present` written for all 32 ports
- Daemon files `/run/wedge100s/sfp_N_eeprom` written for populated ports
- XOR-1 interleave confirmed: port 0 maps to PCA9535 bit 1, port 1 to bit 0
- PCA9535 INPUT is active-low confirmed
- Bus 36 = i2c-36 (PCA9548 ch2), bus 37 = i2c-37 (PCA9548 ch3)

## Remaining Known Gaps

- EEPROM cache is page 0 only (256 bytes). Upper pages (DOM data) are not
  pre-cached. `read_eeprom(offset > 256)` will attempt the sysfs fallback,
  which will likely return `None` in Phase 2 (no optoe driver).
- No TX disable control from host CPU (hardware limitation, not a code gap).
- `get_transceiver_info()` parsing is handled by `SfpOptoeBase` parent class
  using the EEPROM bytes returned by `read_eeprom()`.
- **Vendor string garbled on installed DAC cables (open):** `test_qsfp_eeprom_vendor_info`
  passes on at least one port but the EOS peer reads full vendor strings from
  the same physical modules. Root cause not confirmed; see TEST_PLAN.md
  §Pending Investigation for the four hypotheses to rule out.
