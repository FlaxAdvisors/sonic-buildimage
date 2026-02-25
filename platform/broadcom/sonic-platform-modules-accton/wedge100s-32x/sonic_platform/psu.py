#!/usr/bin/env python3
"""
sonic_platform/psu.py — PSU implementation for Accton Wedge 100S-32X.

Two AC PSUs.  Hardware access is split between two domains:

Presence and power-good:
  System CPLD at host i2c-1/0x32, register 0x10.
  Bit layout (from psui.c / psuutil.py, confirmed on hardware):
    PSU1 present:    bit 0  (0 = present)
    PSU1 power good: bit 1  (1 = good)
    PSU2 present:    bit 4  (0 = present)
    PSU2 power good: bit 5  (1 = good)
  Accessed via subprocess i2cget — no BMC TTY needed.

PMBus telemetry:
  BMC i2c-7; PCA9546 mux at 0x70 selects the PSU channel.
    PSU1: mux channel byte 0x02, PMBus addr 0x59
    PSU2: mux channel byte 0x01, PMBus addr 0x5a
  Mux is selected with a single-byte I2C write (PCA9546 protocol —
  no register address prefix; use 'i2cset BUS ADDR BYTE' form).

  Registers read (LINEAR11 format):
    0x88  READ_VIN   AC input voltage (V)
    0x89  READ_IIN   AC input current (A)
    0x8c  READ_IOUT  DC output current (A)
    0x96  READ_POUT  DC output power (W)

  DC output voltage (VOUT) is computed as POUT/IOUT to avoid
  LINEAR16 VOUT_MODE complexity (mirrors psui.c approach).

Source: psui.c in ONL (OpenNetworkLinux).

Hardware notes (hare-lorax, 2026-02-25):
  - PSU1@0x59 ACKs directly on BMC i2c-7 without mux setup (confirmed
    via i2cdetect); mux setup is still performed for correctness.
  - PSU1 had no AC power in the lab (pgood bit 1 = 0); PSU2 is live.
"""

import subprocess
import time

try:
    from sonic_platform_base.psu_base import PsuBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

try:
    from sonic_platform import bmc
except ImportError:
    from . import bmc


# ---------------------------------------------------------------------------
# Host CPLD constants
# ---------------------------------------------------------------------------

_CPLD_BUS  = 1
_CPLD_ADDR = 0x32
_PSU_REG   = 0x10

# Per-PSU bit positions in register 0x10 (0-indexed arrays, PSU1=index 0)
_PRESENT_BIT = [0, 4]   # 0 = present
_PGOOD_BIT   = [1, 5]   # 1 = power good


# ---------------------------------------------------------------------------
# BMC PMBus constants
# ---------------------------------------------------------------------------

_BMC_MUX_BUS  = 7
_BMC_MUX_ADDR = 0x70

# (mux_channel_byte, pmbus_addr) for each PSU (0-indexed)
_PSU_BMC = [
    (0x02, 0x59),   # PSU1
    (0x01, 0x5a),   # PSU2
]

# PMBus registers (LINEAR11 format)
_REG_VIN  = 0x88
_REG_IIN  = 0x89
_REG_IOUT = 0x8c
_REG_POUT = 0x96

# Telemetry cache: psud polls every ~60 s; 30 s cache reduces BMC load.
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

def _read_cpld_reg():
    """
    Read PSU status byte from host CPLD register 0x10.
    Returns int (0–255) or None on I2C failure.
    """
    try:
        cmd = 'i2cget -f -y {} 0x{:02x} 0x{:02x}'.format(
            _CPLD_BUS, _CPLD_ADDR, _PSU_REG)
        out = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip()
        return int(out, 0)
    except Exception:
        return None


def _set_bmc_mux(channel):
    """
    Select a PCA9546 mux channel on BMC i2c-7 by writing a single byte
    to the mux device (PCA9546 protocol: no register address).

    Uses bmc.send_command() to issue 'i2cset -f -y BUS ADDR BYTE' with
    three arguments — not four — because PCA9546 expects exactly one
    configuration byte after the I2C address, not a register + value pair.

    Returns True on success, False on BMC error.
    """
    cmd = 'i2cset -f -y {} 0x{:02x} 0x{:02x}'.format(
        _BMC_MUX_BUS, _BMC_MUX_ADDR, channel)
    return bmc.send_command(cmd) is not None


def _read_psu_telemetry(psu_idx):
    """
    Read PMBus telemetry for psu_idx (0-based) via BMC TTY.

    Returns a dict with any subset of keys:
      'vin', 'iin', 'iout', 'pout', 'vout', 'ts'
    Missing keys mean the corresponding register read failed.
    Results are cached for _CACHE_TTL seconds.
    """
    now = time.monotonic()
    cached = _psu_cache[psu_idx]
    if cached.get('ts', 0) + _CACHE_TTL > now:
        return cached

    mux_ch, pmbus_addr = _PSU_BMC[psu_idx]

    # Select mux channel; abort cache refresh on failure.
    if not _set_bmc_mux(mux_ch):
        _psu_cache[psu_idx] = {'ts': now}
        return _psu_cache[psu_idx]

    result = {'ts': now}

    def _rw(reg):
        raw = bmc.i2cget_word(_BMC_MUX_BUS, pmbus_addr, reg)
        if raw is None:
            return None
        return _pmbus_decode_linear11(raw)

    vin  = _rw(_REG_VIN)
    iin  = _rw(_REG_IIN)
    iout = _rw(_REG_IOUT)
    pout = _rw(_REG_POUT)

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
        """
        index -- 1-based PSU index (1 = PSU1, 2 = PSU2).
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
        # PMBus MFR_MODEL (0x9a) requires an SMBus block read which is not
        # yet implemented in bmc.py.  Return 'N/A' until Phase 7 adds it.
        return 'N/A'

    def get_serial(self):
        return 'N/A'

    def get_presence(self):
        """True when the PSU is physically inserted (CPLD bit = 0)."""
        val = _read_cpld_reg()
        if val is None:
            return False
        return not bool(val & (1 << _PRESENT_BIT[self._idx]))

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
        """True when the PSU is outputting good power (CPLD bit = 1)."""
        val = _read_cpld_reg()
        if val is None:
            return False
        return bool(val & (1 << _PGOOD_BIT[self._idx]))

    def get_type(self):
        """Wedge 100S uses AC input PSUs."""
        return 'AC'

    def get_capacity(self):
        """Rated capacity in watts (Wedge 100S ships with 650 W PSUs)."""
        return 650.0

    def get_voltage(self):
        """
        DC output voltage in V (computed as POUT / IOUT).
        Returns None when telemetry is unavailable or IOUT is zero.
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
