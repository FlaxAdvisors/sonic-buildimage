# System EEPROM

## Hardware

- Device: 24C64 (8 KiB AT24 EEPROM), ONIE TlvInfo format
- I2C address: `0x50` on bus `i2c-40`
- Physical path: host CP2112 (bus 1) → PCA9548 mux `0x74` → channel 6 → `i2c-40`
- Note: `i2c-40` is a **physical** channel number assigned by the mux tree. In the running
  system (Phase 2), `i2c_mux_pca954x` and `at24` are **not loaded** as kernel modules.
  Bus numbers `i2c-2` through `i2c-41` do not exist in the running kernel.
- Verified EEPROM contents: part `20-001688`, serial `AI09019591`, base MAC `00:90:fb:61:da:a1`,
  vendor `Accton`, manufacturer `Joytech`

## Driver / Daemon

- **Primary path (normal operation):** `wedge100s-i2c-daemon` writes
  `/run/wedge100s/syseeprom` at boot (`OnBootSec=5s`, before `pmon` starts).
  The daemon accesses the EEPROM via `/dev/hidraw0` (CP2112 HID transport),
  navigating the mux tree in userspace. This eliminates mux contention between
  the system EEPROM (mux `0x74` ch6) and QSFP presence chips (mux `0x74` ch2/3)
  that caused `0x51` address corruption and zeroed-data incidents pre-daemon.
- **Fallback path:** Direct sysfs read of `/sys/bus/i2c/devices/40-0050/eeprom`.
  Valid only during the first ~5 s of boot before the daemon writes its cache, or
  if the daemon failed entirely. Requires `i2c_mux_pca954x` and `at24` to be loaded
  (they are not loaded in Phase 2 production).
- The `at24` device is registered by `accton_wedge100s_util.py` at boot via
  `echo 24c64 0x50 > /sys/bus/i2c/devices/i2c-40/new_device` (reference path, not
  used in Phase 2 daemon mode).

## Python API

- **Class:** `SysEeprom` in `sonic_platform/eeprom.py`
- **Inherits:** `eeprom_tlvinfo.TlvInfoDecoder`
- **Key methods:**

| Method | Returns | Primary file read |
|---|---|---|
| `read_eeprom()` | `bytearray` (8192 bytes raw) | `/run/wedge100s/syseeprom`, fallback `/sys/bus/i2c/devices/40-0050/eeprom` |
| `get_eeprom()` | `dict` of TLV entries | calls `read_eeprom()` |
| `system_eeprom_info()` | same dict | delegates to `get_eeprom()` |

- TLV keys are hex type-code strings, e.g. `"0x21"` (Product Name), `"0x22"` (Part Number),
  `"0x23"` (Serial Number), `"0x24"` (Base MAC).
- Results are in-process cached after first successful parse (`self._eeprom_cache`).
- `Chassis.get_system_eeprom_info()` delegates directly to `self._eeprom.get_eeprom()`.
- Magic-byte check (`TlvInfo\x00`) gates acceptance of daemon cache data.
- `use_cache=False` is passed to `TlvInfoDecoder.__init__`; the class manages its own
  cache via the daemon file.

## Pass Criteria

- `/run/wedge100s/syseeprom` exists, is ≥ 8 bytes, and starts with `TlvInfo\x00`
- `SysEeprom().get_eeprom()` returns a non-empty dict
- Dict contains at minimum keys `0x21` (Product Name), `0x22` (Part Number),
  `0x23` (Serial Number), `0x24` (Base MAC Address)
- `sonic-db-cli STATE_DB HGET 'EEPROM_INFO|State' 'Initialized'` returns `'1'`
- `show platform syseeprom` (SONiC CLI) returns populated TLV table

## Known Gaps

- `use_cache=False` disables the `TlvInfoDecoder` parent-class file cache;
  the in-process `_eeprom_cache` dict is lost on every `pmon` restart.
- The sysfs fallback path (`40-0050/eeprom`) only works if `at24` is loaded and
  the mux tree is instantiated; neither is true in Phase 2.
- No write path is implemented; EEPROM fields cannot be updated via Python API.
- `get_model()` and `get_serial()` for the chassis are not wired to EEPROM fields
  in `chassis.py`; they return `'N/A'`.
