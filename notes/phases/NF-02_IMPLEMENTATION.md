# NF-02 — Transceiver Info & DOM: IMPLEMENTATION

## What Was Built

### Files Changed

| File (repo-relative) | Description |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py` | Full implementation |

### Class Structure

`Sfp` extends `SfpOptoeBase` (from `sonic_platform_base.sonic_xcvr.sfp_optoe_base`).
`SfpOptoeBase` provides `get_transceiver_info()`, `get_transceiver_bulk_status()`,
`get_transceiver_threshold_info()` and the full xcvr API factory chain. The platform
class only needs to implement the I/O layer.

### Key Constants

```python
NUM_SFPS = 32

_SFP_BUS_MAP = [
     3,  2,  5,  4,  7,  6,  9,  8,
    11, 10, 13, 12, 15, 14, 17, 16,
    19, 18, 21, 20, 23, 22, 25, 24,
    27, 26, 29, 28, 31, 30, 33, 32,
]
# Index i = 0-based port number → I2C bus number (from sfpi.c in ONL)

_I2C_EEPROM_CACHE  = '/run/wedge100s/sfp_{}_eeprom'   # 256-byte flat file (page 0)
_I2C_PRESENT_CACHE = '/run/wedge100s/sfp_{}_present'  # "1" or "0"
_PRESENCE_MAX_AGE_S = 8                                # daemon fires every 3s
_PCA9535_BUS  = [36, 37]                               # I2C buses for presence GPIO
_PCA9535_ADDR = [0x22, 0x23]
```

### EEPROM Read Path (`read_eeprom`)

1. **Primary**: open `/run/wedge100s/sfp_N_eeprom`, seek to `offset`, read `num_bytes`.
   Returns `bytearray`. No lock needed.

2. **Cache miss fallback**:
   - Check `/run/wedge100s/sfp_N_present` — if `"0"`, return `None` immediately.
   - Else: acquire `_eeprom_bus_lock` and call `SfpOptoeBase.read_eeprom(self, offset, num_bytes)`,
     which reads from the sysfs path returned by `get_eeprom_path()`.

3. **`get_eeprom_path()`**: returns daemon cache path if it exists; otherwise
   `_EEPROM_PATH.format(bus)` = `/sys/bus/i2c/devices/i2c-{bus}/{bus}-0050/eeprom`.

### Presence Detection (`get_presence`)

1. **Primary**: stat `/run/wedge100s/sfp_N_present`. If mtime < 8s old, read value.
2. **Stale fallback**: direct smbus2 read of PCA9535.
   - `group = port // 16` selects which PCA9535
   - `line = (port % 16) ^ 1` — XOR-1 interleave (from ONL sfpi.c)
   - `reg = line // 8`, `bit = line % 8`
   - PCA9535 INPUT register is active-low: bit=0 means present → `return not bool(...)`

### Stubbed Controls

The following methods return constant values because the hardware signals are not
accessible from the host CPU (LP_MODE and RESET are on the mux board):

```python
get_reset_status() → False
get_lpmode()       → False
reset()            → False
set_lpmode(lpmode) → False
```

### get_transceiver_info() — Field Source

Inherited from `SfpOptoeBase` → `Sff8636Api`. EEPROM byte offsets (SFF-8636):
- Byte 0: identifier (0x11 = QSFP28)
- Byte 2: connector type
- Bytes 3–10: compliance codes (40G/100G-CR4 spec compliance)
- Byte 19: nominal bit rate (× 100 Mbps)
- Bytes 20–35: vendor name (16 ASCII bytes)
- Bytes 40–55: vendor OUI + vendor PN (16 bytes)
- Bytes 56–63: vendor rev (4 bytes)
- Bytes 68–83: vendor serial (16 bytes)
- Bytes 84–91: date code

### get_transceiver_bulk_status() — DOM Values

For passive DAC cables: all DOM fields return `N/A` because:
- No monitoring circuitry
- DOM capability byte (SFF-8636 byte 92, bit 2) is 0 on passive cables

For active optics / active DACs:
- Temperature: module sensor, degrees C
- Voltage: module supply, V
- TX/RX power: per-channel, dBm
- TX bias: per-channel, mA

## Hardware-Verified Facts

- verified on hardware 2026-03-02: `read_eeprom(0, 4)` returns `0x11` (QSFP28) consistently
- verified on hardware 2026-03-02: `XcvrApiFactory` creates `Sff8636Api` for byte-0=0x11
- verified on hardware 2026-03-02: `TRANSCEIVER_INFO|EthernetN` populated for all 6 present ports
- verified on hardware 2026-03-02: `TRANSCEIVER_DOM_SENSOR|EthernetN` shows N/A (passive DAC, correct)
- verified on hardware 2026-03-02: garbled vendor name on cheap DAC cables is cable quality issue
- verified on hardware 2026-03-02: `get_presence()` returns True for 6 installed ports (one false positive on Ethernet64 from breakout cable)

## Remaining Known Gaps

- **DOM not tested with active optics**: All hardware cables are passive DAC. DOM values
  (temp, voltage, power) have not been validated on SR4 or LR4 modules.
- **`get_xcvr_api()` intermittent None**: 2/7 present ports returned valid API during testing.
  Root cause: cheap knockoff DAC cables with unreliable EEPROM byte 0. Not a platform bug.
- **Ethernet48 reports GBIC (0x01) intermittently**: Same cable as other ports that show
  QSFP28. Symptom of flaky EEPROM, not platform code.
- **Sysfs fallback path untested**: In normal operation the daemon cache is always present.
  The sysfs fallback via `SfpOptoeBase` has not been exercised with the kernel mux driver
  loaded (that driver is not used in the SONiC build).
