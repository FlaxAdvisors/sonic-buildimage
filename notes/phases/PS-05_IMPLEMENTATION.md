# PS-05 IMPLEMENTATION — System EEPROM

## Files Changed

- `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/eeprom.py`
  (new file)

## File Paths

| Variable | Path |
|---|---|
| `_SYSEEPROM_DAEMON_CACHE` | `/run/wedge100s/syseeprom` |
| `_SYSEEPROM_SYSFS` | `/sys/bus/i2c/devices/40-0050/eeprom` |
| `_ONIE_MAGIC` | `b'TlvInfo\x00'` (8 bytes) |

## Class Design

`SysEeprom` inherits from `eeprom_tlvinfo.TlvInfoDecoder`.

Constructor: `super().__init__(_SYSEEPROM_SYSFS, 0, '', False)` — passes the
sysfs path as the raw EEPROM path, offset 0, empty string for label, and
`use_cache=False` (no file-system caching by the parent; we manage our own).

### `read_eeprom()`

Returns raw EEPROM bytes as `bytearray`.

1. Open `_SYSEEPROM_DAEMON_CACHE` in binary mode, read up to 8192 bytes
2. Validate: `len(data) >= 8 and data[:8] == _ONIE_MAGIC`
3. If valid: return `bytearray(data)`
4. On `OSError` or invalid magic: open `_SYSEEPROM_SYSFS`, read 8192 bytes
5. If both fail: return `None`

### `get_eeprom()`

Returns parsed TLV dict. Implements a permanent in-memory cache via
`self._eeprom_cache`.

Parsing loop:
1. `total_length = (raw[9] << 8) | raw[10]` — TlvInfo header total TLV length
2. `idx = self._TLV_INFO_HDR_LEN` — start of first TLV
3. Loop: `self.is_valid_tlv(raw[idx:])`, extract type/length/value, call
   `self.decoder(None, tlv)` for decoding, store as `result["0xNN"] = value`
4. Stop at CRC_32 TLV type (`self._TLV_CODE_CRC_32`)

Keys use uppercase hex format: `"0x21"`, `"0x22"`, etc.

### `system_eeprom_info()`

Alias for `get_eeprom()`. Required by some SONiC platform callers that use
this method name instead of `get_system_eeprom_info()`.

## Hardware-Verified Facts

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64):
- Serial Number (0x23): `AI09019591`
- MAC Address (0x24): `00:90:fb:61:da:a1`
- Part Number (0x22): `20-001688`
- Product Name (0x21): `WEDGE100S` (or similar — check `decode-syseeprom` output)
- `/run/wedge100s/syseeprom` confirmed present and valid after daemon run
- File size: 8192 bytes (full EEPROM image)
- ONIE magic at offset 0: `54 6c 76 49 6e 66 6f 00` = `TlvInfo\x00`

## Key Decisions

**Magic validation before returning daemon cache data.** A truncated daemon
write (e.g. daemon killed mid-write) would produce garbage data. The 8-byte
magic check catches this and triggers the sysfs fallback.

**8 KiB read.** The 24c64 EEPROM is 8 KiB. Reading the full image ensures all
TLV entries are captured, even non-standard vendor extensions beyond the first
page.

**Permanent in-memory cache.** EEPROM content does not change during normal
operation. Caching permanently avoids repeated file reads on every
`decode-syseeprom` call.

## Remaining Known Gaps

- No CRC verification of the TlvInfo checksum. The `TlvInfoDecoder` base class
  provides `is_checksum_valid()` but it is not called in `get_eeprom()`.
  A corrupted EEPROM would produce incorrect decoded values silently.
- The sysfs fallback (`/sys/bus/i2c/devices/40-0050/eeprom`) requires at24
  driver registration, which is intentionally absent in Phase 2. If the daemon
  cache is unavailable in Phase 2, `get_eeprom()` returns `{}`.
- `write_eeprom()` is not implemented (inherits parent stub returning `False`).
  EEPROM is read-only in normal SONiC operation.
