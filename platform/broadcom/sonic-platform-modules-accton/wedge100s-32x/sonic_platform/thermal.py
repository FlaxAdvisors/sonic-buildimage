#!/usr/bin/env python3
"""
sonic_platform/thermal.py — Thermal sensors for Accton Wedge 100S-32X.

8 sensors (source: thermali.c in ONL):

  Index 0 — CPU Core     host sysfs, coretemp driver, max across all cores
  Index 1 — TMP75-1      BMC i2c-3/0x48
  Index 2 — TMP75-2      BMC i2c-3/0x49
  Index 3 — TMP75-3      BMC i2c-3/0x4a
  Index 4 — TMP75-4      BMC i2c-3/0x4b
  Index 5 — TMP75-5      BMC i2c-3/0x4c
  Index 6 — TMP75-6      BMC i2c-8/0x48
  Index 7 — TMP75-7      BMC i2c-8/0x49

CPU Core is read directly from host sysfs (coretemp driver).
TMP75 sensors live on the OpenBMC I2C buses; they are accessed by sending
'cat <sysfs-path>' to the BMC via /dev/ttyACM0 using bmc.file_read_int().

BMC paths use a hwmon wildcard (hwmonN number is not stable on OpenBMC).
Wildcard in the path is expanded by the BMC shell; bmc.py's _parse_int()
searches for the original (pre-expansion) command string in the echo, so
the wildcard form is safe to pass directly to file_read_int().

Verified on hardware (hare-lorax, SONiC 6.1.0-29-2-amd64, 2026-02-25):
  3-0048 → 23.75 °C,  3-0049 → 22.9 °C,  3-004a → 23.1 °C,
  3-004b → 33.3 °C,   3-004c → 21.1 °C,
  8-0048 → 20.6 °C,   8-0049 → 23.0 °C.
"""

import glob as _glob

try:
    from sonic_platform_base.thermal_base import ThermalBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

try:
    from sonic_platform import bmc
except ImportError:
    from . import bmc


# ---------------------------------------------------------------------------
# Sensor table
#   (name, source, path, high_threshold_C, high_critical_threshold_C)
#
# 'host' — read with Python glob directly from the host filesystem.
# 'bmc'  — read via BMC TTY using bmc.file_read_int(path).
# ---------------------------------------------------------------------------
_SENSORS = [
    # Index 0: CPU Core — report the highest reading across all cores.
    # Broadwell-DE D1508 Tjmax ≈ 105 °C; thresholds match common SONiC policy.
    (
        "CPU Core",
        "host",
        "/sys/devices/platform/coretemp.0/hwmon/hwmon*/temp*_input",
        95.0,
        102.0,
    ),
    # Indices 1–5: mainboard TMP75 sensors on BMC i2c-3.
    ("TMP75-1", "bmc", "/sys/bus/i2c/devices/3-0048/hwmon/*/temp1_input", 70.0, 80.0),
    ("TMP75-2", "bmc", "/sys/bus/i2c/devices/3-0049/hwmon/*/temp1_input", 70.0, 80.0),
    ("TMP75-3", "bmc", "/sys/bus/i2c/devices/3-004a/hwmon/*/temp1_input", 70.0, 80.0),
    ("TMP75-4", "bmc", "/sys/bus/i2c/devices/3-004b/hwmon/*/temp1_input", 70.0, 80.0),
    ("TMP75-5", "bmc", "/sys/bus/i2c/devices/3-004c/hwmon/*/temp1_input", 70.0, 80.0),
    # Indices 6–7: fan-board TMP75 sensors on BMC i2c-8.
    ("TMP75-6", "bmc", "/sys/bus/i2c/devices/8-0048/hwmon/*/temp1_input", 70.0, 80.0),
    ("TMP75-7", "bmc", "/sys/bus/i2c/devices/8-0049/hwmon/*/temp1_input", 70.0, 80.0),
]

NUM_THERMALS = len(_SENSORS)


class Thermal(ThermalBase):
    """Platform-specific Thermal class for Accton Wedge 100S-32X."""

    def __init__(self, index):
        """
        index -- 0-based sensor index:
                   0   = CPU Core (host sysfs)
                   1–7 = TMP75 sensors (BMC sysfs via TTY)
        """
        ThermalBase.__init__(self)
        self._index = index
        self._name, self._source, self._path, self._high, self._high_crit = \
            _SENSORS[index]
        self._min_recorded = None
        self._max_recorded = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_temperature(self):
        """Return current temperature in °C, or None on failure."""
        if self._source == "host":
            return self._read_host_temp_max()
        return self._read_bmc_temp()

    def _read_host_temp_max(self):
        """
        Glob-expand the coretemp sysfs path on the host filesystem and
        return the maximum reading across all matched files (millidegrees
        → degrees).  Mirrors ONL's onlp_file_read_int_max().
        """
        paths = _glob.glob(self._path)
        if not paths:
            return None
        best = None
        for p in paths:
            try:
                with open(p, 'r') as f:
                    val = float(f.read().strip()) / 1000.0
                if best is None or val > best:
                    best = val
            except (IOError, OSError, ValueError):
                pass
        return best

    def _read_bmc_temp(self):
        """
        Read a TMP75 temperature via BMC TTY.
        bmc.file_read_int() sends 'cat <path>' to the BMC shell and parses
        the integer result.  The sysfs value is in millidegrees; divide by
        1000 to get °C.
        """
        raw = bmc.file_read_int(self._path)
        if raw is None:
            return None
        return raw / 1000.0

    def _update_minmax(self, temp):
        if temp is not None:
            if self._min_recorded is None or temp < self._min_recorded:
                self._min_recorded = temp
            if self._max_recorded is None or temp > self._max_recorded:
                self._max_recorded = temp

    # ------------------------------------------------------------------
    # ThermalBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return self._name

    def get_presence(self):
        # All sensors are board-mounted; they are always physically present.
        return True

    def get_model(self):
        return "N/A"

    def get_serial(self):
        return "N/A"

    def get_status(self):
        """True when the sensor is readable (not faulted or absent)."""
        return self._read_temperature() is not None

    def get_temperature(self):
        """
        Returns current temperature in Celsius (float), or None on failure.
        Also updates the min/max recorded values.
        """
        temp = self._read_temperature()
        self._update_minmax(temp)
        return temp

    def get_high_threshold(self):
        return self._high

    def set_high_threshold(self, temperature):
        self._high = float(temperature)
        return True

    def get_high_critical_threshold(self):
        return self._high_crit

    def set_high_critical_threshold(self, temperature):
        self._high_crit = float(temperature)
        return True

    def get_minimum_recorded(self):
        return self._min_recorded

    def get_maximum_recorded(self):
        return self._max_recorded

    def get_position_in_parent(self):
        return self._index + 1

    def is_replaceable(self):
        return False
