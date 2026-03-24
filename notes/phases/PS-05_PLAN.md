# PS-05 PLAN — System EEPROM

## Problem Statement

SONiC requires the system EEPROM (ONIE TlvInfo format) to obtain the chassis
MAC address, serial number, and part number. These values are used at boot time
by `sonic-cfggen` to populate the management interface MAC and to identify the
hardware. Without this subsystem, `decode-syseeprom` fails and LLDP advertises
a zero MAC.

The physical EEPROM is a 24c64 (8 KiB) at I2C address 0x50, behind a PCA9548
mux at 0x74 channel 6, which translates to i2c-40. This is on the CP2112 mux
tree which must not be accessed concurrently with QSFP operations.

## Proposed Approach

**Primary path:** Read from `/run/wedge100s/syseeprom`, a binary file written by
`wedge100s-i2c-daemon` at startup (OnBootSec=5s, before pmon starts). The file
is a raw 8 KiB copy of the EEPROM starting with the ONIE `TlvInfo\x00` magic.

**Fallback path:** Direct sysfs read from `/sys/bus/i2c/devices/40-0050/eeprom`.
This requires the at24 driver to be loaded and the device registered. In Phase 2
(EOS-like daemon), these are not normally active, so the fallback is primarily
for the window between boot and daemon first run.

**TLV parsing:** `SysEeprom` extends `eeprom_tlvinfo.TlvInfoDecoder` from the
SONiC platform library. The `get_eeprom()` method parses the TlvInfo binary
format and returns a dict keyed by hex type codes (e.g. `"0x21"` for Product Name).

## Files to Change

| File | Role |
|---|---|
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/eeprom.py` | SysEeprom class |

`chassis.py` must instantiate `self._eeprom = SysEeprom()` and expose
`get_system_eeprom_info()` returning `self._eeprom.get_eeprom()`.

## Acceptance Criteria

- `chassis.get_system_eeprom_info()` returns a non-empty dict
- Dict contains key `"0x21"` (Product Name) with value containing "WEDGE" or "100S"
- Dict contains key `"0x24"` (MAC Address) with a valid non-zero MAC
- Dict contains key `"0x23"` (Serial Number) with a non-empty string
- Dict contains key `"0x22"` (Part Number) with a non-empty string
- `decode-syseeprom` CLI produces consistent output

## Risks and Watchouts

- **Daemon timing:** The `wedge100s-i2c-daemon` is started at `OnBootSec=5s`.
  If `chassis.py` is imported within the first 5 seconds of boot (unlikely but
  possible), `read_eeprom()` falls back to sysfs, which requires the at24
  device to be registered. This is not registered in Phase 2.
- **Mux contention:** The system EEPROM (mux 0x74 ch6) shares the PCA9548 mux
  with the PCA9535 presence chips (ch2/3). Concurrent reads from chassis.py
  (EEPROM) and a direct smbus2 call (QSFP presence) can corrupt the 0x51
  address and produce zeroed EEPROM data. The daemon cache eliminates this
  race: only the daemon ever touches mux 0x74 after boot.
- **Magic check:** `read_eeprom()` validates the 8-byte ONIE magic
  (`TlvInfo\x00`) before returning data from the daemon cache. A partially
  written cache file will fail this check and trigger the sysfs fallback.
- **In-memory cache:** `_eeprom_cache` in `SysEeprom` caches the parsed dict
  permanently (first-read-wins). If the daemon file is updated after first
  access, the new data is not picked up until pmon restarts.
