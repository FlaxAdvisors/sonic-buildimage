#!/usr/bin/env python3
"""
sonic_platform/fan.py — Fan implementation for Accton Wedge 100S-32X.

5 fan trays, each with a front and rear rotor.  All fan data lives on the
OpenBMC I2C bus 8, fan-board controller at 0x33.

Source: fani.c in ONL (OpenNetworkLinux).

Hardware-verified paths (hare-lorax, SONiC 6.1.0-29-2-amd64, 2026-02-25):
  /sys/bus/i2c/devices/8-0033/fantray_present  — hex bitmask, 0x0 = all present
  /sys/bus/i2c/devices/8-0033/fan1_input       — front rotor, tray 1 (~7500 RPM)
  /sys/bus/i2c/devices/8-0033/fan2_input       — rear  rotor, tray 1 (~4950 RPM)
  ...fan9_input (front, tray 5), fan10_input (rear, tray 5)

fan<N>_input assignment (matching fani.c fid*2-1 / fid*2 formula):
  tray 1: fan1 (front), fan2 (rear)
  tray 2: fan3 (front), fan4 (rear)
  tray 3: fan5 (front), fan6 (rear)
  tray 4: fan7 (front), fan8 (rear)
  tray 5: fan9 (front), fan10 (rear)

fantray_present bitmask: bit (fid-1) SET = tray absent; 0x0 = all present.

Speed is reported as min(front_rpm, rear_rpm) per ONL fani.c policy.
Direction is F2B = FAN_DIRECTION_INTAKE (fixed, per ONL fani.c).
Speed control: 'set_fan_speed.sh <pct>' on the BMC affects all trays.

Caching: fantray_present and per-tray RPM pairs are cached for _CACHE_TTL
seconds to avoid redundant BMC calls when thermalctld reads multiple
attributes of the same fan in a single poll pass.
"""

import time

try:
    from sonic_platform_base.fan_base import FanBase
    from sonic_platform_base.fan_drawer_base import FanDrawerBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

try:
    from sonic_platform import bmc
except ImportError:
    from . import bmc


# ---------------------------------------------------------------------------
# Constants (from fani.c)
# ---------------------------------------------------------------------------

_FAN_BOARD_PATH = '/sys/bus/i2c/devices/8-0033/'
_MAX_FAN_SPEED  = 15400   # RPM at 100 % duty cycle (per fani.c)
NUM_FANS        = 5       # 5 fan trays


# ---------------------------------------------------------------------------
# Module-level state shared across all Fan instances
# ---------------------------------------------------------------------------

# Target speed tracks the last value passed to set_speed().  All 5 trays
# are controlled by a single set_fan_speed.sh call so one variable suffices.
# None means thermalctld has not yet issued a set_speed(); get_target_speed()
# raises NotImplementedError when None so that thermalctld's try_get() returns
# NOT_AVAILABLE and skips is_under/over_speed checks (avoids false "Not OK"
# alarms before the first explicit speed command).
_target_speed_pct = None

# Short-lived cache so that multiple attribute reads in one thermalctld pass
# hit the BMC only once per unique sysfs path.
_CACHE_TTL = 2.0   # seconds

_fantray_cache = {'ts': 0.0, 'val': None}   # fantray_present bitmask
_rpm_cache     = {}                           # {fan_index: {'ts', 'front', 'rear'}}


def _cached_fantray_present():
    """
    Return the fantray_present bitmask (hex), or None on BMC error.
    Result is cached for _CACHE_TTL seconds; all 5 Fan objects share it.
    """
    now = time.monotonic()
    if (_fantray_cache['val'] is not None
            and now - _fantray_cache['ts'] < _CACHE_TTL):
        return _fantray_cache['val']
    val = bmc.file_read_int(_FAN_BOARD_PATH + 'fantray_present', base=16)
    _fantray_cache['ts'] = now
    _fantray_cache['val'] = val
    return val


def _cached_rpm_pair(fan_index):
    """
    Return (front_rpm, rear_rpm) for fan tray fan_index (1-based).
    Each pair is cached for _CACHE_TTL seconds.
    Returns (None, None) when the BMC is unreadable.
    """
    now   = time.monotonic()
    entry = _rpm_cache.get(fan_index)
    if entry and now - entry['ts'] < _CACHE_TTL:
        return entry['front'], entry['rear']

    front_path = _FAN_BOARD_PATH + 'fan{}_input'.format(fan_index * 2 - 1)
    rear_path  = _FAN_BOARD_PATH + 'fan{}_input'.format(fan_index * 2)
    front = bmc.file_read_int(front_path, base=10)
    rear  = bmc.file_read_int(rear_path,  base=10)
    _rpm_cache[fan_index] = {'ts': now, 'front': front, 'rear': rear}
    return front, rear


# ---------------------------------------------------------------------------
# Fan class
# ---------------------------------------------------------------------------

class Fan(FanBase):
    """Platform-specific Fan class for Accton Wedge 100S-32X."""

    def __init__(self, fan_index):
        """
        fan_index -- 1-based fan tray index (1–5), matching ONL fid.
        """
        FanBase.__init__(self)
        self.index = fan_index   # 1-based, 1–5

    # ------------------------------------------------------------------
    # DeviceBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return 'Chassis Fan - {}'.format(self.index)

    def get_model(self):
        return 'N/A'

    def get_serial(self):
        return 'N/A'

    def get_presence(self):
        """
        True when the fan tray is physically installed.

        fantray_present bit layout (from fani.c):
          bit (fid-1) SET   → tray absent
          bit (fid-1) CLEAR → tray present
          0x00              → all trays present
        """
        bitmask = _cached_fantray_present()
        if bitmask is None:
            return False
        return not bool(bitmask & (1 << (self.index - 1)))

    def get_status(self):
        """True when the tray is present and at least one rotor is spinning."""
        if not self.get_presence():
            return False
        rpm = self.get_speed_rpm()
        return rpm is not None and rpm > 0

    def get_position_in_parent(self):
        return self.index

    def is_replaceable(self):
        return True

    # ------------------------------------------------------------------
    # FanBase API
    # ------------------------------------------------------------------

    def get_direction(self):
        """F2B (front-to-back) = INTAKE.  Fixed per fani.c."""
        return FanBase.FAN_DIRECTION_INTAKE

    def get_speed(self):
        """
        Current fan speed as a percentage of MAX_FAN_SPEED (0–100).

        Uses min(front_rpm, rear_rpm) per fani.c policy.
        Returns 0 when the tray is absent, stalled, or BMC is unreadable.
        """
        rpm = self.get_speed_rpm()
        if not rpm:
            return 0
        return min((rpm * 100) // _MAX_FAN_SPEED, 100)

    def get_speed_rpm(self):
        """
        Current fan speed in RPM.

        Reports min(front_rotor, rear_rotor), matching fani.c which flags
        a tray as failed when any rotor stalls.  Returns 0 if a rotor has
        stalled; returns None when the BMC is unreadable.
        """
        front, rear = _cached_rpm_pair(self.index)
        if front is None and rear is None:
            return None
        available = [v for v in (front, rear) if v is not None]
        return min(available)

    def get_target_speed(self):
        """
        Last speed percentage set via set_speed().

        Raises NotImplementedError before the first set_speed() call so that
        thermalctld's try_get() falls back to NOT_AVAILABLE and skips the
        is_under/over_speed checks — avoiding false "Not OK" alarms on the
        first poll cycle before thermalctld has issued any speed command.

        All trays share one target value because set_fan_speed.sh controls
        the whole fan board simultaneously.
        """
        if _target_speed_pct is None:
            raise NotImplementedError
        return _target_speed_pct

    def get_speed_tolerance(self):
        """
        Allowable deviation from target speed (percentage points) before
        an alarm is raised.  20 % matches common Accton platform policy.
        """
        return 20

    def set_speed(self, speed):
        """
        Set all fan trays to speed % of maximum.

        Sends 'set_fan_speed.sh <pct>' to the BMC via TTY.  The script
        controls the PWM for all 5 trays simultaneously.

        Returns True on success, False on failure.
        """
        global _target_speed_pct
        speed = max(0, min(100, int(speed)))
        result = bmc.send_command('set_fan_speed.sh {}'.format(speed))
        if result is not None:
            _target_speed_pct = speed
            # Invalidate RPM cache so next read reflects the new speed.
            _rpm_cache.clear()
            return True
        return False

    def set_status_led(self, color):
        """Fan tray LEDs are not individually addressable on this platform."""
        return False

    def get_status_led(self):
        """Fan tray LEDs are not individually addressable on this platform."""
        return FanBase.STATUS_LED_COLOR_OFF


# ---------------------------------------------------------------------------
# FanDrawer — one per fan tray; contains a single Fan object.
#
# thermalctld iterates chassis.get_all_fan_drawers() and then calls
# drawer.get_all_fans() for each drawer.  The Wedge 100S-32X has 5 fan
# trays; we model each as a FanDrawer holding one Fan (the combined
# min(front, rear) reading that fani.c reports for the tray).
# ---------------------------------------------------------------------------

class FanDrawer(FanDrawerBase):
    """Platform-specific FanDrawer class for Accton Wedge 100S-32X."""

    def __init__(self, drawer_index):
        """
        drawer_index -- 1-based fan tray index (1–5).
        """
        FanDrawerBase.__init__(self)
        self.index = drawer_index
        self._fan_list.append(Fan(drawer_index))

    # ------------------------------------------------------------------
    # DeviceBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return 'FanTray {}'.format(self.index)

    def get_model(self):
        return 'N/A'

    def get_serial(self):
        return 'N/A'

    def get_presence(self):
        """True when the fan tray is physically inserted (shares Fan logic)."""
        return self._fan_list[0].get_presence()

    def get_status(self):
        """True when the tray is present and at least one rotor is spinning."""
        return self._fan_list[0].get_status()

    def get_position_in_parent(self):
        return self.index

    def is_replaceable(self):
        return True

    # ------------------------------------------------------------------
    # FanDrawerBase API
    # ------------------------------------------------------------------

    def set_status_led(self, color):
        """Fan tray LEDs are not individually addressable on this platform."""
        return False

    def get_status_led(self):
        """Fan tray LEDs are not individually addressable on this platform."""
        return 'off'
