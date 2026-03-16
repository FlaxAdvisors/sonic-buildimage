# NF-02 — Transceiver Info & DOM: PLAN

## Problem Statement

SONiC's xcvrd daemon needs per-port transceiver data to populate `STATE_DB`:
- `TRANSCEIVER_INFO|EthernetN` — module identity (vendor, PN, serial, connector, wavelength)
- `TRANSCEIVER_DOM_SENSOR|EthernetN` — real-time optical power, temperature, voltage, bias
- `TRANSCEIVER_STATUS|EthernetN` — flag/status fields

This data comes from EEPROM page 0 (bytes 0–255) read via I2C from the module.
The Wedge 100S-32X has no on-chip I2C-to-PCIe bridge; the CP2112 USB-HID chip and
an i2c_mux_pca954x tree provides I2C access in the ONL model, but in the SONiC port
the mux driver is replaced by a userspace daemon that caches EEPROM data in
`/run/wedge100s/sfp_N_eeprom`.

The challenge: xcvrd calls the platform API's `read_eeprom()` method, which must
either hit the daemon cache or fall back to sysfs — but sysfs paths only exist if
the kernel mux driver is loaded (it is not, in the current design).

## Proposed Approach

1. Implement `Sfp.read_eeprom(offset, num_bytes)` to read from daemon cache
   `/run/wedge100s/sfp_N_eeprom` (256 bytes, page 0) as the primary path.
2. Fallback: if cache file is absent, check presence; if present, attempt
   sysfs read via `SfpOptoeBase.read_eeprom()` under a bus-serialization lock.
3. Implement `Sfp.get_presence()` similarly: daemon cache primary,
   smbus2 PCA9535 read fallback.
4. Inherit `get_transceiver_info()`, `get_transceiver_bulk_status()`, and
   `get_transceiver_threshold_info()` from `SfpOptoeBase` / `XcvrApi` —
   these parse EEPROM bytes per SFF-8636 automatically once `read_eeprom()` works.

## Files to Change

| File | Action |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py` | Implement (primary) |

## Acceptance Criteria

- `TRANSCEIVER_INFO|EthernetN` populated in STATE_DB for all inserted modules
- `TRANSCEIVER_DOM_SENSOR|EthernetN` populated (N/A for passive DAC, real values for active optics)
- `get_transceiver_info()` returns dict with at least: `type`, `manufacturer`, `model`,
  `serial`, `connector`, `dom_capability`
- `get_xcvr_api()` returns a non-None `Sff8636Api` object for QSFP28 modules
- No I2C bus hangs from concurrent xcvrd + daemon access

## Risks and Watch-Outs

- **Passive DAC cables**: Vendor name, PN, and serial fields will be garbled or empty on
  cheap/knockoff DAC cables. This is a cable quality issue, not a platform bug. Tests must
  tolerate None/garbled vendor fields for the current hardware.
- **EEPROM byte 0 caching artifact**: Direct `cat` of sysfs eeprom sometimes returns `0x01`
  (GBIC) due to kernel read caching. The platform API `read_eeprom()` consistently returns
  `0x11` (QSFP28). Always use the API, never raw file reads, in tests.
- **DOM for passive DAC = all N/A**: Passive cables have no DOM electronics. This is correct.
  Do not test DOM values against plausible ranges on this hardware unless active optics
  are installed.
- **Bus lock required for fallback sysfs path**: The CP2112 USB-HID bus is shared and
  does not handle concurrent access. The `_eeprom_bus_lock` (RLock) must be held for
  the fallback path. Daemon cache path does not need the lock (pure file I/O).
- **XOR-1 interleave in presence detection**: The PCA9535 GPIO lines are wired with a
  one-bit XOR interleave: line = (port % 16) ^ 1. Derived from ONL sfpi.c. Incorrectly
  computing this causes phantom present/absent readings.
