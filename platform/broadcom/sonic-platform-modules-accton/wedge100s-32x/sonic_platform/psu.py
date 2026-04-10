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
  Files contain raw PMBus word values as plain decimal integers:
    psu_{1,2}_vin   READ_VIN             (0x88) LINEAR11 → V
    psu_{1,2}_iin   READ_IIN             (0x89) LINEAR11 → A
    psu_{1,2}_iout  READ_IOUT            (0x8c) LINEAR11 → A
    psu_{1,2}_pout  READ_POUT            (0x96) LINEAR11 → W
    psu_{1,2}_vout  READ_VOUT            (0x8b) LINEAR16 → V (Delta
                    SPAFCBK-14G reports VOUT_MODE=0x17, exp=-9, so
                    volts = raw / 512.0; verified on hardware 2026-04-09)
    psu_{1,2}_temp  READ_TEMPERATURE_1   (0x8d) LINEAR11 → °C
    psu_{1,2}_fan   READ_FAN_SPEED_1     (0x90) raw RPM — Delta
                    SPAFCBK-14G reports plain RPM as a 16-bit word,
                    not LINEAR11 or duty cycle (verified on hardware
                    2026-04-09: both PSUs ≈10k-10.5k RPM at idle load)

  For PSUs on firmware that does not expose READ_VOUT, DC output voltage
  is computed as POUT/IOUT as a fallback.

Source: psui.c in ONL (OpenNetworkLinux), extended with direct reads.

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

_PSU_ALARM_CACHE    = '/run/wedge100s/psu{}_alarm'
_PSU_INPUT_OK_CACHE = '/run/wedge100s/psu{}_input_ok'
_PSU_MODEL_CACHE    = '/run/wedge100s/psu_{}_model'
_PSU_SERIAL_CACHE   = '/run/wedge100s/psu_{}_serial'

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


def _linear16_vout_to_volts(raw):
    """Decode PMBus READ_VOUT (LINEAR16) to volts for the Delta SPAFCBK-14G.

    The Delta SPAFCBK-14G reports VOUT_MODE (0x20) = 0x17, i.e. signed 5-bit
    exponent = -9, so the formula is simply ``volts = raw / 512.0``. Verified
    on hardware 2026-04-09 against both PSUs: PSU1 raw=0x17ca → 11.895 V,
    PSU2 raw=0x1802 → 12.004 V.

    Args:
        raw: 16-bit unsigned integer from READ_VOUT (0x8B).

    Returns:
        float: Voltage in volts.
    """
    return raw / 512.0


def _read_psu_telemetry(psu_idx):
    """
    Read PMBus telemetry for psu_idx (0-based) from daemon output files.

    wedge100s-bmc-daemon writes raw PMBus words (decimal) to:
      /run/wedge100s/psu_{N}_{vin,iin,iout,pout,vout,temp,fan}
    where N = psu_idx + 1 (1-based).

    Returns a dict with any subset of keys:
      'vin', 'iin', 'iout', 'pout', 'vout', 'temp', 'fan_rpm', 'ts'
    Missing keys mean the corresponding file was unreadable.
    ``vout`` is taken from the direct READ_VOUT cache when available and
    falls back to ``pout / iout`` otherwise.
    Results are cached for _CACHE_TTL seconds.
    """
    now = time.monotonic()
    cached = _psu_cache[psu_idx]
    if cached.get('ts', 0) + _CACHE_TTL > now:
        return cached

    psu_n  = psu_idx + 1   # 1-based for file names
    result = {'ts': now}

    def _rf_linear11(reg_name):
        raw = _read_daemon_int('{}/psu_{}_{}'.format(_RUN_DIR, psu_n, reg_name))
        if raw is None:
            return None
        return _pmbus_decode_linear11(raw)

    def _rf_raw(reg_name):
        return _read_daemon_int('{}/psu_{}_{}'.format(_RUN_DIR, psu_n, reg_name))

    vin  = _rf_linear11('vin')
    iin  = _rf_linear11('iin')
    iout = _rf_linear11('iout')
    pout = _rf_linear11('pout')
    temp = _rf_linear11('temp')

    if vin  is not None: result['vin']  = vin
    if iin  is not None: result['iin']  = iin
    if iout is not None: result['iout'] = iout
    if pout is not None: result['pout'] = pout
    if temp is not None: result['temp'] = temp

    # Prefer direct READ_VOUT (LINEAR16, exp=-9 for Delta SPAFCBK-14G).
    # Fall back to POUT/IOUT if the vout cache file is missing or unreadable.
    vout_raw = _rf_raw('vout')
    if vout_raw is not None:
        result['vout'] = _linear16_vout_to_volts(vout_raw)
    elif iout is not None and pout is not None and iout > 0.0:
        result['vout'] = pout / iout

    # READ_FAN_SPEED_1 on Delta SPAFCBK-14G is plain RPM (not LINEAR11),
    # stored as a raw 16-bit integer in the daemon cache.
    fan_rpm = _rf_raw('fan')
    if fan_rpm is not None:
        result['fan_rpm'] = fan_rpm

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
        """Return PSU model string from PMBus MFR_MODEL (0x9A).

        Returns:
            str: Model string, or "N/A" if unavailable.
        """
        path = _PSU_MODEL_CACHE.format(self._index)
        try:
            with open(path) as f:
                model = f.read().strip()
            return model if model else "N/A"
        except OSError:
            return "N/A"

    def get_serial(self):
        """Return PSU serial number from PMBus MFR_SERIAL (0x9E).

        Returns:
            str: Serial number string, or "N/A" if unavailable.
        """
        path = _PSU_SERIAL_CACHE.format(self._index)
        try:
            with open(path) as f:
                serial = f.read().strip()
            return serial if serial else "N/A"
        except OSError:
            return "N/A"

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

        Prefers the direct READ_VOUT (0x8B) value from the daemon cache
        (LINEAR16 decoded with the Delta SPAFCBK-14G exponent of -9, i.e.
        ``raw / 512``) and falls back to POUT / IOUT if the direct read
        is unavailable.

        Returns:
            float: DC output voltage in V, or None if unavailable.
        """
        return _read_psu_telemetry(self._idx).get('vout')

    def get_temperature(self):
        """Return PSU intake air temperature in degrees Celsius.

        Reads the raw PMBus READ_TEMPERATURE_1 (0x8D) LINEAR11 word from
        /run/wedge100s/psu_<N>_temp and decodes it per the PMBus spec.

        Returns:
            float: Temperature in degrees Celsius, or None on read failure.
        """
        return _read_psu_telemetry(self._idx).get('temp')

    def get_num_fans(self):
        """Return the number of fans exposed by this PSU.

        Delta SPAFCBK-14G PSUs report FAN_1 installed and FAN_2 absent in
        PMBus FAN_CONFIG_1_2 (0x3a = 0x90), so exactly one fan is exposed.

        Returns:
            int: Number of fans (1).
        """
        return 1

    def get_all_fans(self):
        """Return the list of fans belonging to this PSU.

        Returns:
            list[PsuFan]: Single-element list containing the PSU-internal fan.
                The PsuFan instance is cached per-Psu so repeated calls return
                the same object.
        """
        if not hasattr(self, '_fans') or not self._fans:
            self._fans = [PsuFan(self._index)]
        return self._fans

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

    def get_psu_alarm(self):
        """Return True if PSU has an active alarm condition.

        Returns:
            bool: True if alarm active (abnormal), False if normal.
        """
        path = _PSU_ALARM_CACHE.format(self._index)
        try:
            with open(path) as f:
                # CPLD: 1=normal, 0=alarm — invert for "has alarm" semantics
                return f.read().strip() == '0'
        except OSError:
            return False

    def get_input_status(self):
        """Return True if PSU input power is OK.

        Returns:
            bool: True if input power is within acceptable range.
        """
        path = _PSU_INPUT_OK_CACHE.format(self._index)
        try:
            with open(path) as f:
                return f.read().strip() == '1'
        except OSError:
            return False

    def set_status_led(self, color):
        return False

    def get_status_led(self):
        if self.get_status():
            return PsuBase.STATUS_LED_COLOR_GREEN
        return PsuBase.STATUS_LED_COLOR_RED


# ---------------------------------------------------------------------------
# PsuFan — PSU-internal cooling fan reported via PMBus READ_FAN_SPEED_1
# ---------------------------------------------------------------------------

try:
    from sonic_platform_base.fan_base import FanBase
except ImportError as _e:  # pragma: no cover - base class must exist in SONiC
    raise ImportError(str(_e) + " - required module not found")


# Nominal full-speed RPM for the Delta SPAFCBK-14G internal fan. Used only
# to derive the percentage returned by get_speed(); the RPM reading itself
# comes straight from READ_FAN_SPEED_1. If actual max-RPM data becomes
# available (e.g. from a cooling spec or a loaded-PSU measurement) update
# this constant — it is not used for any safety-critical decision.
_PSU_FAN_MAX_RPM = 20000


class PsuFan(FanBase):
    """Platform-specific PSU-internal fan for Accton Wedge 100S-32X.

    Reports the READ_FAN_SPEED_1 (PMBus 0x90) word written by
    wedge100s-bmc-daemon into /run/wedge100s/psu_<N>_fan. On the Delta
    SPAFCBK-14G the register returns plain RPM as an unsigned 16-bit
    integer (verified on hardware 2026-04-09 against both PSUs), not
    LINEAR11 or duty cycle, so get_speed_rpm() returns the raw value.
    """

    def __init__(self, psu_index):
        """Initialize the PSU fan wrapper.

        Args:
            psu_index: 1-based parent PSU index (1 = PSU1, 2 = PSU2).
        """
        FanBase.__init__(self)
        self._psu_index = psu_index          # 1-based
        self._psu_idx   = psu_index - 1     # 0-based for telemetry array

    # ------------------------------------------------------------------
    # DeviceBase API
    # ------------------------------------------------------------------

    def get_name(self):
        """Return the SONiC-visible fan name (PSU<N>_FAN1)."""
        return 'PSU{}_FAN1'.format(self._psu_index)

    def get_model(self):
        """PSU fans are not field-replaceable and report no discrete model."""
        return 'N/A'

    def get_serial(self):
        """PSU fans do not expose a dedicated serial number."""
        return 'N/A'

    def get_presence(self):
        """Present iff the parent PSU is present.

        Returns:
            bool: True when the parent PSU is inserted.
        """
        val = _read_cpld_attr('psu{}_present'.format(self._psu_index))
        return bool(val) if val is not None else False

    def get_status(self):
        """True when the parent PSU is present and the fan is spinning."""
        if not self.get_presence():
            return False
        rpm = self.get_speed_rpm()
        return rpm is not None and rpm > 0

    def get_position_in_parent(self):
        """Return the 1-based position of this fan within the parent PSU."""
        return 1

    def is_replaceable(self):
        """PSU fans are not independently replaceable (bundled with PSU)."""
        return False

    # ------------------------------------------------------------------
    # FanBase API
    # ------------------------------------------------------------------

    def get_direction(self):
        """PSU fans follow the chassis front-to-back airflow."""
        return FanBase.FAN_DIRECTION_INTAKE

    def get_speed(self):
        """Return the current fan speed as a percentage of _PSU_FAN_MAX_RPM.

        Returns:
            int: Speed percentage (0–100). Returns 0 if the PSU is absent
                or the RPM read failed.
        """
        rpm = self.get_speed_rpm()
        if not rpm:
            return 0
        return min((rpm * 100) // _PSU_FAN_MAX_RPM, 100)

    def get_speed_rpm(self):
        """Return the current fan speed in RPM.

        Reads the raw READ_FAN_SPEED_1 word from the bmc-daemon cache.
        The Delta SPAFCBK-14G reports plain RPM, so no decode is needed.

        Returns:
            int: RPM value, or None if the cache file is unreadable.
        """
        return _read_psu_telemetry(self._psu_idx).get('fan_rpm')

    def get_target_speed(self):
        """Return the current fan speed as the target (BMC-managed PSU).

        PSU fan speed is managed autonomously by the PSU firmware; no
        runtime target can be set from the host. Reporting the current
        RPM (as a percentage) as the target keeps SONiC system-health
        under/over-speed checks satisfied.

        Returns:
            int: Target fan speed percentage (0–100).
        """
        return self.get_speed()

    def get_speed_tolerance(self):
        """Return the acceptable speed tolerance as a percentage.

        PSU firmware manages cooling autonomously; a generous 50 %
        tolerance prevents false under/over-speed alarms in system-health.

        Returns:
            int: Speed tolerance percentage.
        """
        return 50

    def set_speed(self, speed):
        """Setting PSU fan speed from the host is not supported."""
        return False

    def set_status_led(self, color):
        """PSU fans have no independent status LED."""
        return False

    def get_status_led(self):
        """Return the parent PSU status LED color."""
        if self.get_status():
            return FanBase.STATUS_LED_COLOR_GREEN
        return FanBase.STATUS_LED_COLOR_RED
