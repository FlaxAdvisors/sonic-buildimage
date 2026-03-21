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
  page 0) on insertion events only.  sfp.py serves reads from this cache.
  When xcvrd requests DOM data (lower page, bytes 0-127) and the cache is
  older than _DOM_CACHE_TTL seconds, sfp.py does a live lower-page read
  via smbus2, updates the cache file, and resets the TTL timer.  The upper
  page (bytes 128-255, static vendor info) is never re-read after insertion.

Source: sfpi.c in ONL (OpenNetworkLinux), confirmed on hare-lorax hardware.
"""

import os
import time
import threading

try:
    from sonic_platform_base.sonic_xcvr.sfp_optoe_base import SfpOptoeBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

try:
    from smbus2 import SMBus, i2c_msg as _i2c_msg
    _SMBUS2_OK = True
except ImportError:
    _SMBUS2_OK = False

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
_LP_MODE_STATE = '/run/wedge100s/sfp_{}_lpmode'
_LP_MODE_REQ   = '/run/wedge100s/sfp_{}_lpmode_req'

# Staleness threshold: fall back to live smbus2 if cache is older than this.
# Daemon fires every 3 s; 8 s gives ~2.5 cycles of slack before triggering
# the fallback (handles daemon slow-start and brief I2C errors).
_PRESENCE_MAX_AGE_S = 8

# PCA9535 buses and addresses (direct smbus2 fallback — presence)
_PCA9535_BUS  = [36, 37]
_PCA9535_ADDR = [0x22, 0x23]

# ---------------------------------------------------------------------------
# Demand-driven DOM cache TTL
#
# Lower-page bytes 0-127 contain live DOM monitoring registers (temperature,
# voltage, Tx/Rx power, bias current).  When xcvrd requests EEPROM data in
# this range and the last hardware read is older than _DOM_CACHE_TTL seconds,
# sfp.py performs a fresh smbus2 read of the lower page, updates the cache
# file, and resets _DOM_LAST_REFRESH[port].
#
# Starting at 0.0 ensures the first read after boot/insertion always triggers
# a fresh hardware fetch regardless of when the daemon wrote the initial cache.
# ---------------------------------------------------------------------------

_DOM_CACHE_TTL      = 10              # seconds: max age of lower-page DOM data
_DOM_LAST_REFRESH   = [0.0] * NUM_SFPS  # monotonic timestamp of last live read

# ---------------------------------------------------------------------------
# Phase 2: daemon cache is the authoritative EEPROM path.
# ---------------------------------------------------------------------------

_EEPROM_PATH = '/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom'  # Phase 1 sysfs (fallback)

# ---------------------------------------------------------------------------
# QSFP mux topology (from wedge100s-i2c-daemon.c bus_to_mux_addr/channel)
#
# Four PCA9548 muxes on CP2112 bus 1, each with 8 channels:
#   mux 0x70 ch0-7 → ONL buses  2- 9  (QSFP ports)
#   mux 0x71 ch0-7 → ONL buses 10-17  (QSFP ports)
#   mux 0x72 ch0-7 → ONL buses 18-25  (QSFP ports)
#   mux 0x73 ch0-7 → ONL buses 26-33  (QSFP ports)
# channel = (bus - 2) % 8
# ---------------------------------------------------------------------------

def _mux_for_bus(bus):
    """Return (mux_i2c_addr, channel) for a QSFP EEPROM bus index, or (None, None)."""
    for base, addr in [(2, 0x70), (10, 0x71), (18, 0x72), (26, 0x73)]:
        if base <= bus < base + 8:
            return addr, (bus - 2) % 8
    return None, None


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
        Return EEPROM bytes, refreshing the lower page on-demand when TTL expires.

        Primary path: /run/wedge100s/sfp_N_eeprom written by wedge100s-i2c-daemon
        on insertion.  No I2C transaction in the normal (TTL-fresh) case.

        DOM refresh: if offset falls in the lower page (0-127) and the cache is
        older than _DOM_CACHE_TTL seconds, performs a live smbus2 read of only
        the lower 128 bytes, merges with the cached upper page, atomically
        replaces the cache file, and resets _DOM_LAST_REFRESH[port].

        Fallback (daemon not yet run, or eeprom file absent on port insertion):
          - If port is known absent (sfp_N_present == "0"): return None immediately.
          - Otherwise: direct sysfs read under _eeprom_bus_lock.
        """
        cache = _I2C_EEPROM_CACHE.format(self._port)

        cached_data = None
        try:
            with open(cache, 'rb') as f:
                raw = f.read(256)
                if len(raw) == 256:
                    cached_data = bytearray(raw)
        except OSError:
            pass

        if cached_data is not None:
            # Demand-driven lower-page refresh when TTL has expired.
            if offset < 128 and (time.monotonic() - _DOM_LAST_REFRESH[self._port]) > _DOM_CACHE_TTL:
                lower = self._hardware_read_lower_page()
                if lower is not None and len(lower) == 128:
                    merged = bytearray(lower) + bytearray(cached_data[128:])
                    tmp = cache + '.tmp'
                    try:
                        with open(tmp, 'wb') as f:
                            f.write(merged)
                        os.replace(tmp, cache)
                    except OSError:
                        merged = cached_data  # write failed; serve old data
                    _DOM_LAST_REFRESH[self._port] = time.monotonic()
                    cached_data = merged
            end = min(offset + num_bytes, 256)
            return cached_data[offset:end]

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

    def _hardware_read_eeprom(self):
        """Read full 256-byte page 0 directly from hardware via smbus2 and the mux tree.

        Route: CP2112 bus 1 → PCA9548 mux (0x70-0x73) → I2C addr 0x50.
        Used by write_eeprom() for post-write cache refresh and by read_eeprom()
        during INIT_SETTLE verification.

        Returns bytearray(256) on success, None on any I2C error.
        Acquires _eeprom_bus_lock; safe to call from outside any lock context.
        """
        if not _SMBUS2_OK:
            return None
        mux_addr, mux_chan = _mux_for_bus(self._bus)
        if mux_addr is None:
            return None
        try:
            with _eeprom_bus_lock:
                with SMBus(1) as i2c:
                    i2c.write_byte(mux_addr, 1 << mux_chan)

                    # Lower page (bytes 0-127): set register pointer to 0x00.
                    lower_rw = [_i2c_msg.write(0x50, [0x00]),
                                _i2c_msg.read(0x50, 128)]
                    i2c.i2c_rdwr(*lower_rw)
                    lower = bytes(lower_rw[1])

                    # Upper page 0 (bytes 128-255): set pointer to 0x80.
                    upper_rw = [_i2c_msg.write(0x50, [0x80]),
                                _i2c_msg.read(0x50, 128)]
                    i2c.i2c_rdwr(*upper_rw)
                    upper = bytes(upper_rw[1])

                    i2c.write_byte(mux_addr, 0x00)  # deselect mux

            return bytearray(lower + upper)

        except Exception:
            try:
                with SMBus(1) as i2c:
                    i2c.write_byte(mux_addr, 0x00)
            except Exception:
                pass
            return None

    def _hardware_read_lower_page(self):
        """Read lower page (bytes 0-127) directly from hardware via smbus2.

        Route: CP2112 bus 1 → PCA9548 mux (0x70-0x73) → I2C addr 0x50.
        Used by read_eeprom() for demand-driven DOM cache refresh.

        Returns bytearray(128) on success, None on any I2C error.
        Acquires _eeprom_bus_lock.
        """
        if not _SMBUS2_OK:
            return None
        mux_addr, mux_chan = _mux_for_bus(self._bus)
        if mux_addr is None:
            return None
        try:
            with _eeprom_bus_lock:
                with SMBus(1) as i2c:
                    i2c.write_byte(mux_addr, 1 << mux_chan)
                    lower_rw = [_i2c_msg.write(0x50, [0x00]),
                                _i2c_msg.read(0x50, 128)]
                    i2c.i2c_rdwr(*lower_rw)
                    lower = bytes(lower_rw[1])
                    i2c.write_byte(mux_addr, 0x00)
            return bytearray(lower)
        except Exception:
            try:
                with SMBus(1) as i2c:
                    i2c.write_byte(mux_addr, 0x00)
            except Exception:
                pass
            return None

    def write_eeprom(self, offset, num_bytes, write_buffer):
        """
        Write to QSFP EEPROM via physical I2C, then refresh the daemon cache.

        Route: smbus2 on CP2112 bus 1 → PCA9548 mux (0x70-0x73) → I2C addr 0x50.
        Always writes to hardware regardless of cached value, then re-reads both
        128-byte pages via _hardware_read_eeprom() and atomically replaces the
        daemon cache file.  This keeps the non-polling architecture while ensuring
        xcvrd control writes (TX_DISABLE, high-power class, etc.) actually reach
        the module and the cache reflects hardware state.

        Note: CP2112 i2c-dev (this path) and the daemon's hidraw0 path share the
        same physical bus without kernel-level serialization.  _eeprom_bus_lock
        serializes within xcvrd; the daemon's 3-second timer creates natural gaps.
        """
        if not _SMBUS2_OK or num_bytes <= 0 or write_buffer is None:
            return False
        if not (0 <= offset < 256):
            return False

        mux_addr, mux_chan = _mux_for_bus(self._bus)
        if mux_addr is None:
            return False

        cache = _I2C_EEPROM_CACHE.format(self._port)

        try:
            with _eeprom_bus_lock:
                with SMBus(1) as i2c:
                    # Select mux channel for this port's EEPROM bus.
                    i2c.write_byte(mux_addr, 1 << mux_chan)

                    # Write registers.  write_i2c_block_data sends:
                    #   START [0x50|W] [offset] [byte0 byte1 ...] STOP
                    i2c.write_i2c_block_data(
                        0x50, offset, list(write_buffer[:num_bytes]))

                    time.sleep(0.05)  # allow EEPROM write cycle to complete

                    # Deselect mux before releasing bus context.
                    i2c.write_byte(mux_addr, 0x00)

            # Re-read full page 0 from hardware and atomically replace cache.
            hw_data = self._hardware_read_eeprom()
            if hw_data is not None:
                tmp = cache + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(hw_data)
                os.replace(tmp, cache)
            return True

        except Exception:
            # Best-effort mux deselect on error.
            try:
                with SMBus(1) as i2c:
                    i2c.write_byte(mux_addr, 0x00)
            except Exception:
                pass
            return False

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
        cache_file = _I2C_PRESENT_CACHE.format(self._port)
        present = None
        try:
            st = os.stat(cache_file)
            if (time.monotonic() - st.st_mtime) < _PRESENCE_MAX_AGE_S:
                with open(cache_file) as f:
                    present = f.read().strip() == '1'
            # Cache stale — fall through to live read
        except OSError:
            pass  # file not yet written (first ~5 s of boot)

        if present is None:
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
            present = not bool((byte >> bit) & 1)  # active-low

        return present

    def get_status(self):
        return self.get_presence()

    def get_position_in_parent(self):
        return self._port + 1

    def is_replaceable(self):
        return True

    # ------------------------------------------------------------------
    # QSFP control
    # RESET is not accessible from host CPU on Wedge 100S-32X.
    # LP_MODE is managed by wedge100s-i2c-daemon via request/state files.
    # ------------------------------------------------------------------

    def get_reset_status(self):
        """Reset pin is not accessible from host CPU; return False."""
        return False

    def get_lpmode(self):
        """
        Return LP_MODE state from daemon state file.

        Returns True if LP_MODE is asserted (low-power, TX off),
        False if deasserted (high-power, TX enabled).

        If the state file does not exist (daemon not yet run), returns True
        (conservative: hardware default is asserted via PCB pull-ups).
        """
        state_file = _LP_MODE_STATE.format(self._port)
        try:
            with open(state_file) as f:
                return f.read().strip() == '1'
        except OSError:
            return True  # hardware default: all LP_MODE asserted at boot

    def reset(self):
        """Reset not supported from host CPU on this platform."""
        return False

    def set_lpmode(self, lpmode):
        """
        Request LP_MODE change by writing a request file for the daemon.

        lpmode=True  → write "1" to sfp_N_lpmode_req (assert, force low-power)
        lpmode=False → write "0" to sfp_N_lpmode_req (deassert, allow high-power)

        The daemon reads and applies the request within one poll cycle (~3 s),
        then deletes the request file and updates the state file.

        Returns True immediately on successful file write (async: hardware state
        changes after the next daemon tick, ~3 s later).

        xcvrd contract: on this platform xcvrd calls set_lpmode() but does not
        re-read LP_MODE state to verify the result; it trusts get_lpmode() on the
        next poll cycle.  The ~3 s async window is acceptable because the daemon
        tick interval matches xcvrd's ~3 s poll period.
        """
        req_file = _LP_MODE_REQ.format(self._port)
        try:
            with open(req_file, 'w') as f:
                f.write('1' if lpmode else '0')
            return True
        except OSError:
            return False

    def get_error_description(self):
        if not self.get_presence():
            return self.SFP_STATUS_UNPLUGGED
        return self.SFP_STATUS_OK
