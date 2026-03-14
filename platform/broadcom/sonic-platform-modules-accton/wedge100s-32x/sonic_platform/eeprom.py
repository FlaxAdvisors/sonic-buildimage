#!/usr/bin/env python3
"""
sonic_platform/eeprom.py — System EEPROM for Accton Wedge 100S-32X.

Hardware: 24c64 (8 KiB) at 0x50 on mux 0x74 ch6 (i2c-40), ONIE TlvInfo format.
ONIE dmesg: "at24 7-0050: 8192 byte 24c64 EEPROM, writable, 1 bytes/write"

The at24 device is registered by accton_wedge100s_util.py at boot via:
    echo 24c64 0x50 > /sys/bus/i2c/devices/i2c-40/new_device
Sysfs node: /sys/bus/i2c/devices/40-0050/eeprom

Primary source: /run/wedge100s/syseeprom written by wedge100s-i2c-daemon at
first boot (OnBootSec=5s, before pmon starts).  This eliminates CP2112 mux
contention: the system EEPROM (mux 0x74 ch6) and PCA9535 presence chips
(mux 0x74 ch2/3) share the same PCA9548 0x74 mux; concurrent reads cause
the 0x51 address corruption and zeroed-data incidents observed pre-daemon.
"""

try:
    from sonic_platform_base.sonic_eeprom import eeprom_tlvinfo
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

_SYSEEPROM_DAEMON_CACHE = '/run/wedge100s/syseeprom'
_SYSEEPROM_SYSFS        = '/sys/bus/i2c/devices/40-0050/eeprom'
_ONIE_MAGIC             = b'TlvInfo\x00'


class SysEeprom(eeprom_tlvinfo.TlvInfoDecoder):

    def __init__(self):
        # Initialize TlvInfoDecoder with sysfs as the raw path.
        # use_cache=False — we manage our own cache via the daemon file.
        super(SysEeprom, self).__init__(_SYSEEPROM_SYSFS, 0, '', False)
        self._eeprom_cache = None

    def read_eeprom(self):
        """
        Return raw EEPROM bytes.

        Primary: /run/wedge100s/syseeprom written by wedge100s-i2c-daemon.
        Fallback: direct sysfs read (first ~5 s of boot, or daemon failed).
        """
        try:
            with open(_SYSEEPROM_DAEMON_CACHE, 'rb') as f:
                data = f.read(8192)
            if len(data) >= 8 and data[:8] == _ONIE_MAGIC:
                return bytearray(data)
        except OSError:
            pass

        # Fallback: read directly from sysfs
        try:
            with open(_SYSEEPROM_SYSFS, 'rb') as f:
                return bytearray(f.read(8192))
        except OSError:
            return None

    def get_eeprom(self):
        """
        Returns a dictionary of TLV entries decoded from the system EEPROM.

        Keys are hex type-code strings (e.g. "0x21" for Product Name).
        Values are their decoded string representations.
        Returns {} on any read or parse error.
        """
        if self._eeprom_cache is not None:
            return self._eeprom_cache

        try:
            raw = self.read_eeprom()
        except Exception:
            return {}

        if raw is None or len(raw) < self._TLV_INFO_HDR_LEN + 2:
            return {}

        total_length = (raw[9] << 8) | raw[10]
        idx = self._TLV_INFO_HDR_LEN
        end = idx + total_length
        result = {}

        while (idx + 2) <= len(raw) and idx < end:
            if not self.is_valid_tlv(raw[idx:]):
                break
            tlv_len = raw[idx + 1]
            tlv = raw[idx:idx + 2 + tlv_len]
            code = "0x{:02X}".format(raw[idx])
            _, value = self.decoder(None, tlv)
            result[code] = value
            if raw[idx] == self._TLV_CODE_CRC_32:
                break
            idx += 2 + tlv_len

        self._eeprom_cache = result
        return result

    def system_eeprom_info(self):
        return self.get_eeprom()
