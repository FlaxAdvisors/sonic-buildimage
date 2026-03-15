#!/usr/bin/env python3
"""
sonic_platform/sfp.py — QSFP28 implementation for Accton Wedge 100S-32X.

All 32 ports are QSFP28 (100G).  Hardware access (normal operation):

Presence:
  wedge100s-i2c-daemon writes /run/wedge100s/sfp_N_present ("0" or "1")
  every 3 s by polling PCA9535 via i2c-dev ioctl.  sfp.py reads these
  files; falls back to direct smbus2 if daemon files are stale (>8 s).

EEPROM:
  wedge100s-i2c-daemon writes /run/wedge100s/sfp_N_eeprom (256 bytes,
  page 0) on insertion events.  sfp.py reads this file; falls back to
  direct optoe1 sysfs read if cache absent (first ~5 s of boot).

Source: sfpi.c in ONL (OpenNetworkLinux), confirmed on hare-lorax hardware.
"""

import os
import time
import threading

try:
    from sonic_platform_base.sonic_xcvr.sfp_optoe_base import SfpOptoeBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

# ---------------------------------------------------------------------------
# CP2112 bus serialization lock
#
# Used only in the sysfs fallback path of read_eeprom().  In normal operation
# (daemon cache hit) this lock is not acquired.  Kept as RLock for the
# SfpOptoeBase re-entrant call pattern (read_eeprom → get_optoe_current_page
# → read_eeprom when offset ≥ 128).
# ---------------------------------------------------------------------------

_eeprom_bus_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Port-to-bus mapping (from sfp_bus_index[] in sfpi.c, 0-based port index)
# ---------------------------------------------------------------------------

NUM_SFPS = 32

_SFP_BUS_MAP = [
     3,  2,  5,  4,  7,  6,  9,  8,
    11, 10, 13, 12, 15, 14, 17, 16,
    19, 18, 21, 20, 23, 22, 25, 24,
    27, 26, 29, 28, 31, 30, 33, 32,
]

# ---------------------------------------------------------------------------
# Daemon cache paths
# ---------------------------------------------------------------------------

_I2C_EEPROM_CACHE  = '/run/wedge100s/sfp_{}_eeprom'
_I2C_PRESENT_CACHE = '/run/wedge100s/sfp_{}_present'

# Staleness threshold: fall back to live smbus2 if cache is older than this.
# Daemon fires every 3 s; 8 s gives ~2.5 cycles of slack before triggering
# the fallback (handles daemon slow-start and brief I2C errors).
_PRESENCE_MAX_AGE_S = 8

# PCA9535 buses and addresses (direct smbus2 fallback)
_PCA9535_BUS  = [36, 37]
_PCA9535_ADDR = [0x22, 0x23]

# ---------------------------------------------------------------------------
# Phase 2: daemon cache is the authoritative EEPROM path.
# ---------------------------------------------------------------------------

_EEPROM_PATH = '/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom'  # Phase 1 sysfs (fallback)


# ---------------------------------------------------------------------------
# Sfp class
# ---------------------------------------------------------------------------

class Sfp(SfpOptoeBase):
    """Platform-specific Sfp class for Accton Wedge 100S-32X (QSFP28 ports)."""

    def __init__(self, port):
        """
        port -- 0-based port index (0–31).
        """
        SfpOptoeBase.__init__(self)
        self._port = port
        self._bus  = _SFP_BUS_MAP[port]

    # ------------------------------------------------------------------
    # SfpOptoeBase interface
    # ------------------------------------------------------------------

    def get_eeprom_path(self):
        """Return the EEPROM path that xcvrd and callers should use.

        Phase 2: daemon cache is primary. Sysfs does not exist (i2c_mux_pca954x
        not loaded). Return the daemon cache path if it exists; fall back to
        sysfs path so the caller gets a predictable non-None string.
        """
        cache = _I2C_EEPROM_CACHE.format(self._port)
        import os
        if os.path.exists(cache):
            return cache
        return _EEPROM_PATH.format(self._bus)

    def read_eeprom(self, offset, num_bytes):
        """
        Return EEPROM bytes from daemon cache, falling back to direct sysfs.

        Primary path: /run/wedge100s/sfp_N_eeprom written by wedge100s-i2c-daemon.
        No I2C transaction in the normal case.

        Fallback (daemon not yet run, or eeprom file absent on port insertion):
          - If port is known absent (sfp_N_present == "0"): return None immediately.
          - Otherwise: direct sysfs read under _eeprom_bus_lock.
        """
        cache = _I2C_EEPROM_CACHE.format(self._port)
        try:
            with open(cache, 'rb') as f:
                f.seek(offset)
                data = f.read(num_bytes)
                if len(data) == num_bytes:
                    return bytearray(data)
        except OSError:
            pass

        # Cache miss — check presence before attempting sysfs fallback.
        present_file = _I2C_PRESENT_CACHE.format(self._port)
        try:
            with open(present_file) as f:
                if f.read().strip() == '0':
                    return None  # absent: nothing to read
        except OSError:
            pass  # presence cache missing → first boot, try sysfs anyway

        # Fallback: direct sysfs read (daemon not yet run, or eeprom not yet cached).
        with _eeprom_bus_lock:
            return SfpOptoeBase.read_eeprom(self, offset, num_bytes)

    # ------------------------------------------------------------------
    # DeviceBase / SfpBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return 'QSFP28 {}'.format(self._port + 1)

    def get_presence(self):
        """
        True when a QSFP28 module is physically inserted in this port.

        Primary path: reads /run/wedge100s/sfp_N_present written by
        wedge100s-i2c-daemon.  File mtime is checked: if older than
        _PRESENCE_MAX_AGE_S the daemon is considered stale and a live
        smbus2 read of the PCA9535 is performed instead.

        XOR-1 interleave: port → line = (port % 16) ^ 1, per ONL sfpi.c.
        PCA9535 INPUT is active-low (bit=0 means present).
        """
        cache = _I2C_PRESENT_CACHE.format(self._port)
        try:
            st = os.stat(cache)
            if (time.monotonic() - st.st_mtime) < _PRESENCE_MAX_AGE_S:
                with open(cache) as f:
                    return f.read().strip() == '1'
            # Cache stale — fall through to live read
        except OSError:
            pass  # file not yet written (first ~5 s of boot)

        # Fallback: direct smbus2 read of PCA9535
        from sonic_platform import platform_smbus
        group = self._port // 16
        line  = (self._port % 16) ^ 1      # XOR-1 interleave (ONL sfpi.c)
        reg   = line // 8
        bit   = line % 8
        byte  = platform_smbus.read_byte(
            _PCA9535_BUS[group], _PCA9535_ADDR[group], reg)
        if byte is None:
            return False
        return not bool((byte >> bit) & 1)  # active-low

    def get_status(self):
        return self.get_presence()

    def get_position_in_parent(self):
        return self._port + 1

    def is_replaceable(self):
        return True

    # ------------------------------------------------------------------
    # QSFP control — not wired to host CPU on Wedge 100S-32X
    # (LP_MODE and RESET are on the mux board, not directly accessible)
    # ------------------------------------------------------------------

    def get_reset_status(self):
        """Reset pin is not accessible from host CPU; return False."""
        return False

    def get_lpmode(self):
        """LP_MODE pin is not accessible from host CPU; return False."""
        return False

    def reset(self):
        """Reset not supported from host CPU on this platform."""
        return False

    def set_lpmode(self, lpmode):
        """LP_MODE not controllable from host CPU on this platform."""
        return False

    def get_error_description(self):
        if not self.get_presence():
            return self.SFP_STATUS_UNPLUGGED
        return self.SFP_STATUS_OK
