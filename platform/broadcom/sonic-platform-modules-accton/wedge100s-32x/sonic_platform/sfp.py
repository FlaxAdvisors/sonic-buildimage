#!/usr/bin/env python3
"""
sonic_platform/sfp.py — QSFP28 implementation for Accton Wedge 100S-32X.

All 32 ports are QSFP28 (100G).  All hardware access goes through
wedge100s-i2c-daemon via /run/wedge100s/ files.  sfp.py never touches
the I2C bus directly.

Presence:
  wedge100s-i2c-daemon writes /run/wedge100s/sfp_N_present ("0" or "1")
  every 3 s by polling PCA9535 via i2c-dev ioctl.  sfp.py reads these
  files; returns False if the file is absent (daemon not yet started).

EEPROM:
  wedge100s-i2c-daemon writes /run/wedge100s/sfp_N_eeprom (256 bytes,
  page 0) on insertion events only.  sfp.py serves reads from this cache.
  When xcvrd requests DOM data (lower page, bytes 0-127) and the cache is
  older than _DOM_CACHE_TTL seconds, sfp.py requests a fresh lower-page
  read via daemon request/response files, updates the cache, and resets
  the TTL timer.  The upper page (bytes 128-255, static vendor info) is
  never re-read after insertion.

Source: sfpi.c in ONL (OpenNetworkLinux), confirmed on hare-lorax hardware.
"""

import os
import time

try:
    from sonic_platform_base.sonic_xcvr.sfp_optoe_base import SfpOptoeBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


NUM_SFPS = 32

# ---------------------------------------------------------------------------
# Daemon cache paths
# ---------------------------------------------------------------------------

_I2C_EEPROM_CACHE  = '/run/wedge100s/sfp_{}_eeprom'
_I2C_PRESENT_CACHE = '/run/wedge100s/sfp_{}_present'
_LP_MODE_STATE = '/run/wedge100s/sfp_{}_lpmode'
_LP_MODE_REQ   = '/run/wedge100s/sfp_{}_lpmode_req'
_WRITE_REQ  = '/run/wedge100s/sfp_{}_write_req'   # pmon → daemon: JSON {offset, length, data_hex}
_WRITE_ACK  = '/run/wedge100s/sfp_{}_write_ack'   # daemon → pmon: "ok" or "err:<msg>"
_READ_REQ   = '/run/wedge100s/sfp_{}_read_req'    # pmon → daemon: JSON {offset, length}
_READ_RESP  = '/run/wedge100s/sfp_{}_read_resp'   # daemon → pmon: hex-encoded bytes or "err:<msg>"
_WRITE_TIMEOUT_S = 5.0
_READ_TIMEOUT_S  = 5.0

# ---------------------------------------------------------------------------
# Demand-driven DOM cache TTL
#
# Lower-page bytes 0-127 contain live DOM monitoring registers (temperature,
# voltage, Tx/Rx power, bias current).  When xcvrd requests EEPROM data in
# this range and the last refresh is older than _DOM_CACHE_TTL seconds,
# sfp.py sends a read request to wedge100s-i2c-daemon, which performs the
# I2C read and writes the response; sfp.py merges the result into the cache.
#
# Starting at 0.0 ensures the first read after boot/insertion always triggers
# a fresh fetch regardless of when the daemon wrote the initial cache.
# ---------------------------------------------------------------------------

_DOM_CACHE_TTL      = 20              # seconds: max staleness per port
_DOM_LAST_REFRESH   = [0.0] * NUM_SFPS  # monotonic timestamp of last live read
_POWER_INIT_MTIME: dict = {}           # port → eeprom mtime when Power Override was last written

def _wait_for_file(path, timeout_s):
    """Poll path until it exists; return True on success, False on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


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

    # ------------------------------------------------------------------
    # SfpOptoeBase interface
    # ------------------------------------------------------------------

    def get_eeprom_path(self):
        """Return the daemon cache path for this port's EEPROM."""
        return _I2C_EEPROM_CACHE.format(self._port)

    def read_eeprom(self, offset, num_bytes):
        """
        Return EEPROM bytes from the daemon cache, refreshing DOM on TTL expiry.

        Normal path: reads /run/wedge100s/sfp_N_eeprom written by
        wedge100s-i2c-daemon on insertion.  No I2C transaction unless TTL expires.

        DOM refresh: if offset falls in the lower page (0-127) and the cache is
        older than _DOM_CACHE_TTL seconds, requests a fresh lower-page read via
        daemon request/response files, merges with the cached upper page, and
        atomically replaces the cache file.

        Returns None if the cache file is absent (port not yet seen by daemon —
        normal during the first few seconds after pmon start, or port is empty).
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

        if cached_data is None:
            return None

        self._init_power_override(cached_data)

        # Demand-driven lower-page refresh when TTL has expired.
        if offset < 128 and (time.monotonic() - _DOM_LAST_REFRESH[self._port]) > _DOM_CACHE_TTL:
            lower = self._hardware_read_lower_page()
            # Always reset TTL regardless of read success/failure.  If the
            # daemon is busy, serve stale cached data rather than hammering
            # the request on every subsequent read_eeprom() call.
            _DOM_LAST_REFRESH[self._port] = time.monotonic()
            if lower is not None and len(lower) == 128:
                merged = bytearray(lower) + bytearray(cached_data[128:])
                tmp = cache + '.tmp'
                try:
                    with open(tmp, 'wb') as f:
                        f.write(merged)
                    os.replace(tmp, cache)
                except OSError:
                    merged = cached_data  # write failed; serve old data
                cached_data = merged

        end = min(offset + num_bytes, 256)
        return cached_data[offset:end]

    def _hardware_read_lower_page(self):
        """Read lower page (bytes 0-127) from hardware via daemon read request file.

        Returns bytearray(128) on success, None on timeout or error.
        """
        req_path  = _READ_REQ.format(self._port)
        resp_path = _READ_RESP.format(self._port)

        payload = {"offset": 0, "length": 128}
        try:
            os.unlink(resp_path)
        except OSError:
            pass

        import json as _json
        try:
            tmp = req_path + '.tmp'
            with open(tmp, 'w') as f:
                f.write(_json.dumps(payload))
            os.replace(tmp, req_path)
        except OSError:
            return None

        if not _wait_for_file(resp_path, _READ_TIMEOUT_S):
            try:
                os.unlink(req_path)
            except OSError:
                pass
            return None

        try:
            with open(resp_path) as f:
                result = f.read().strip()
            os.unlink(resp_path)
        except OSError:
            return None

        if result.startswith("err:"):
            return None
        try:
            data = bytes.fromhex(result)
            return bytearray(data) if len(data) == 128 else None
        except ValueError:
            return None

    def _init_power_override(self, cached_data):
        """Set Power Override (byte 93 bit 1) on first EEPROM read after insertion.

        SFF-8636 byte 129 bits 7-6 = 0b11 means Power Class 4+ (≥3.5 W).
        Without Power Override=1 these modules stay in a reduced-power idle
        state and the laser does not fire even with lpmode deasserted.

        Keyed on eeprom file mtime so re-insertion triggers a fresh write.
        Preserves CDR-control bits (3-2); clears Power Set (bit 0) so the
        module runs at full rated power.
        """
        cache_path = _I2C_EEPROM_CACHE.format(self._port)
        try:
            mtime = os.path.getmtime(cache_path)
        except OSError:
            return
        if _POWER_INIT_MTIME.get(self._port) == mtime:
            return

        if len(cached_data) >= 130 and ((cached_data[129] >> 6) & 0x03) == 0x03:
            byte93 = cached_data[93]
            if not (byte93 & 0x02):
                self.write_eeprom(93, 1, bytearray([(byte93 | 0x02) & ~0x01]))

        _POWER_INIT_MTIME[self._port] = mtime

    def write_eeprom(self, offset, num_bytes, write_buffer):
        """Write to QSFP EEPROM via daemon request file; wait for ack."""
        if num_bytes <= 0 or write_buffer is None:
            return False
        if not (0 <= offset < 256):
            return False

        req_path = _WRITE_REQ.format(self._port)
        ack_path = _WRITE_ACK.format(self._port)

        payload = {
            "offset": offset,
            "length": num_bytes,
            "data_hex": bytes(write_buffer[:num_bytes]).hex()
        }
        # Remove stale ack from any prior request.
        try:
            os.unlink(ack_path)
        except OSError:
            pass

        import json as _json
        try:
            tmp = req_path + '.tmp'
            with open(tmp, 'w') as f:
                f.write(_json.dumps(payload))
            os.replace(tmp, req_path)
        except OSError:
            return False

        if not _wait_for_file(ack_path, _WRITE_TIMEOUT_S):
            # Timeout — daemon did not respond.
            try:
                os.unlink(req_path)
            except OSError:
                pass
            return False

        try:
            with open(ack_path) as f:
                result = f.read().strip()
            os.unlink(ack_path)
        except OSError:
            return False

        return result == "ok"

    # ------------------------------------------------------------------
    # DeviceBase / SfpBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return 'QSFP28 {}'.format(self._port + 1)

    def get_presence(self):
        """
        True when a QSFP28 module is physically inserted in this port.

        Reads /run/wedge100s/sfp_N_present written by wedge100s-i2c-daemon
        every 3 s.  Returns False if the file is absent (daemon not yet
        started — normal during the first few seconds after pmon start).
        """
        cache_file = _I2C_PRESENT_CACHE.format(self._port)
        try:
            with open(cache_file) as f:
                return f.read().strip() == '1'
        except OSError:
            return False

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
