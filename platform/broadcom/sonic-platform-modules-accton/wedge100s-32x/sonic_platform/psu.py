#!/usr/bin/env python3
"""
sonic_platform/psu.py — PSU implementation for Accton Wedge 100S-32X.

Two AC PSUs.  Hardware access is split between two domains:

Presence and power-good:
  Read from /run/wedge100s/ files written by wedge100s-i2c-daemon (poll_cpld):
    psu1_present  — 1 = present (driver inverts active-low bit 0 of reg 0x10)
    psu1_pgood    — 1 = power good (bit 1, active-high)
    psu2_present  — 1 = present (driver inverts active-low bit 4 of reg 0x10)
    psu2_pgood    — 1 = power good (bit 5, active-high)
  Requires wedge100s_cpld kernel module (Phase R26) and wedge100s-i2c-daemon.

PMBus telemetry (Phase R29):
  Read from /run/wedge100s/ files written by wedge100s-bmc-daemon (R28).
  Files contain raw LINEAR11 16-bit words as plain decimal integers:
    psu_{1,2}_vin   READ_VIN  (0x88) AC input voltage (V)
    psu_{1,2}_iin   READ_IIN  (0x89) AC input current (A)
    psu_{1,2}_iout  READ_IOUT (0x8c) DC output current (A)
    psu_{1,2}_pout  READ_POUT (0x96) DC output power (W)

  DC output voltage (VOUT) is computed as POUT/IOUT to avoid
  LINEAR16 VOUT_MODE complexity (mirrors psui.c approach).

Source: psui.c in ONL (OpenNetworkLinux).

Hardware notes (hare-lorax, 2026-02-25):
  - PSU1 had no AC power in the lab (pgood bit 1 = 0); PSU2 is live.
"""

import time

try:
    from sonic_platform_base.psu_base import PsuBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


# ---------------------------------------------------------------------------
# Daemon output directory (wedge100s-bmc-poller, Phase R28)
# Files: psu_{1,2}_{vin,iin,iout,pout} — raw LINEAR11 word, decimal integer.
# ---------------------------------------------------------------------------

_RUN_DIR = '/run/wedge100s'

# Telemetry cache: psud polls every ~60 s; 30 s cache reduces read load.
_CACHE_TTL  = 30.0
_psu_cache  = [{} for _ in range(2)]   # one dict per PSU (0-indexed)

NUM_PSUS = 2


# ---------------------------------------------------------------------------
# PMBus LINEAR11 decoder
# ---------------------------------------------------------------------------

def _pmbus_decode_linear11(raw):
    """
    Decode a PMBus LINEAR11 16-bit word to a float value.

    Format:
      bits [15:11] — 5-bit two's complement exponent  N
      bits [10:0]  — 11-bit two's complement mantissa Y
    Value = Y × 2^N

    Mirrors pmbus_parse_literal_format() in psui.c, but returns a float
    in base SI units (V / A / W) rather than milli-units (SONiC API
    expects V/A/W directly).
    """
    exp_raw = (raw >> 11) & 0x1f
    exp = exp_raw if exp_raw < 16 else exp_raw - 32      # 5-bit twos-comp

    man_raw = raw & 0x7ff
    man = man_raw if man_raw < 1024 else man_raw - 2048  # 11-bit twos-comp

    if exp >= 0:
        return float(man << exp)
    else:
        return float(man) / float(1 << (-exp))


# ---------------------------------------------------------------------------
# Hardware access helpers
# ---------------------------------------------------------------------------

def _read_cpld_attr(name):
    """Read a CPLD integer attribute from the daemon cache (/run/wedge100s/)."""
    try:
        with open('{}/{}'.format(_RUN_DIR, name)) as f:
            return int(f.read().strip(), 0)
    except Exception:
        return None


def _read_daemon_int(path):
    """Read a plain decimal integer from a bmc-poller daemon output file."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (IOError, OSError, ValueError):
        return None


def _read_psu_telemetry(psu_idx):
    """
    Read PMBus telemetry for psu_idx (0-based) from daemon output files.

    wedge100s-bmc-daemon writes raw LINEAR11 words (decimal) to:
      /run/wedge100s/psu_{N}_{vin,iin,iout,pout}
    where N = psu_idx + 1 (1-based).

    Returns a dict with any subset of keys:
      'vin', 'iin', 'iout', 'pout', 'vout', 'ts'
    Missing keys mean the corresponding file was unreadable.
    Results are cached for _CACHE_TTL seconds.
    """
    now = time.monotonic()
    cached = _psu_cache[psu_idx]
    if cached.get('ts', 0) + _CACHE_TTL > now:
        return cached

    psu_n  = psu_idx + 1   # 1-based for file names
    result = {'ts': now}

    def _rf(reg_name):
        raw = _read_daemon_int('{}/psu_{}_{}'.format(_RUN_DIR, psu_n, reg_name))
        if raw is None:
            return None
        return _pmbus_decode_linear11(raw)

    vin  = _rf('vin')
    iin  = _rf('iin')
    iout = _rf('iout')
    pout = _rf('pout')

    if vin  is not None: result['vin']  = vin
    if iin  is not None: result['iin']  = iin
    if iout is not None: result['iout'] = iout
    if pout is not None: result['pout'] = pout

    # Compute VOUT = POUT / IOUT (avoids LINEAR16 VOUT_MODE complexity).
    # Skip if IOUT is zero to avoid division by zero at no load.
    if iout is not None and pout is not None and iout > 0.0:
        result['vout'] = pout / iout

    _psu_cache[psu_idx] = result
    return result


# ---------------------------------------------------------------------------
# Psu class
# ---------------------------------------------------------------------------

class Psu(PsuBase):
    """Platform-specific PSU class for Accton Wedge 100S-32X."""

    def __init__(self, index):
        """Initialize PSU instance.

        Args:
            index: 1-based PSU index (1 = PSU1, 2 = PSU2).
        """
        PsuBase.__init__(self)
        self._index = index       # 1-based
        self._idx   = index - 1  # 0-based for array indexing

    # ------------------------------------------------------------------
    # DeviceBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return 'PSU-{}'.format(self._index)

    def get_model(self):
        """Return PSU model. Static string — PMBus block-read not implemented."""
        return "Delta DPS-1100AB-6 A"

    def get_serial(self):
        return 'N/A'

    def get_presence(self):
        """Check if the PSU is physically inserted.

        Returns:
            bool: True when present, False when absent or CPLD unreadable.
        """
        val = _read_cpld_attr('psu{}_present'.format(self._index))
        if val is None:
            return False
        return bool(val)

    def get_status(self):
        """True when the PSU is present and power is good."""
        return self.get_presence() and self.get_powergood_status()

    def get_position_in_parent(self):
        return self._index

    def is_replaceable(self):
        return True

    # ------------------------------------------------------------------
    # PsuBase API
    # ------------------------------------------------------------------

    def get_powergood_status(self):
        """True when the PSU is outputting good power."""
        val = _read_cpld_attr('psu{}_pgood'.format(self._index))
        if val is None:
            return False
        return bool(val)

    def get_type(self):
        """Wedge 100S uses AC input PSUs."""
        return 'AC'

    def get_capacity(self):
        """Rated capacity in watts (Wedge 100S ships with 650 W PSUs)."""
        return 650.0

    def get_voltage(self):
        """Return DC output voltage in volts.

        Computed as POUT / IOUT to avoid LINEAR16 VOUT_MODE complexity.

        Returns:
            float: DC output voltage in V, or None if unavailable.
        """
        return _read_psu_telemetry(self._idx).get('vout')

    def get_current(self):
        """DC output current in A (READ_IOUT, PMBus reg 0x8c)."""
        return _read_psu_telemetry(self._idx).get('iout')

    def get_power(self):
        """DC output power in W (READ_POUT, PMBus reg 0x96)."""
        return _read_psu_telemetry(self._idx).get('pout')

    def get_input_voltage(self):
        """AC input voltage in V (READ_VIN, PMBus reg 0x88)."""
        return _read_psu_telemetry(self._idx).get('vin')

    def get_input_current(self):
        """AC input current in A (READ_IIN, PMBus reg 0x89)."""
        return _read_psu_telemetry(self._idx).get('iin')

    def set_status_led(self, color):
        return False

    def get_status_led(self):
        if self.get_status():
            return PsuBase.STATUS_LED_COLOR_GREEN
        return PsuBase.STATUS_LED_COLOR_RED
