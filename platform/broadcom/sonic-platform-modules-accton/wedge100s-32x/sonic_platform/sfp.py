#!/usr/bin/env python3
"""
sonic_platform/sfp.py — QSFP28 implementation for Accton Wedge 100S-32X.

All 32 ports are QSFP28 (100G).  Hardware access:

Presence:
  PCA9535 GPIO expanders on host I2C:
    i2c-36/0x22: ports 0-15  (mux 0x74 ch2)
    i2c-37/0x23: ports 16-31 (mux 0x74 ch3)
  Each PCA9535 has two 8-bit input registers (offset 0 and 1).
  The GPIO bit order is wired in interleaved even/odd order; apply
  bit_swap() per ONL sfpi.c onlp_sfpi_reg_val_to_port_sequence().
  Bit value 0 = module present (active low).

EEPROM:
  Each QSFP28 EEPROM is at I2C addr 0x50 on its own bus (see _SFP_BUS_MAP).
  The bus is a mux channel from one of the 5 PCA9548 muxes on i2c-1.
  Device is registered lazily using the optoe1 driver (optoe module must
  be loaded — see accton_wedge100s_util.py kos list).
  Sysfs path: /sys/bus/i2c/devices/i2c-{bus}/{bus}-0050/eeprom

DOM:
  DOM data accessible at I2C addr 0x51 on the same bus (same mux channel).
  For optoe1-based devices, upper pages are accessed via the sysfs file.

Source: sfpi.c in ONL (OpenNetworkLinux), confirmed on hare-lorax hardware.

Hardware notes (hare-lorax, 2026-02-25):
  - Port 0 (bus 3): QSFP28 present, identifier 0x11 confirmed.
  - Ports 1-31: absent in lab setup.
  - PCA9535 gpio chip bound at i2c-36/37 (gpiochip2/3) after Phase 1 init.
"""

import os
import subprocess
import time

try:
    from sonic_platform_base.sonic_xcvr.sfp_optoe_base import SfpOptoeBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")


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
# PCA9535 presence registers
# ---------------------------------------------------------------------------

# (bus, i2c_addr) indexed by group:  group 0 = ports 0-15, group 1 = ports 16-31
_PRESENCE_BUS  = [36, 37]
_PRESENCE_ADDR = [0x22, 0x23]

# ---------------------------------------------------------------------------
# Sysfs paths for EEPROM device instantiation
# ---------------------------------------------------------------------------

_EEPROM_PATH = '/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom'
_NEW_DEVICE  = '/sys/bus/i2c/devices/i2c-{}/new_device'

# ---------------------------------------------------------------------------
# Presence cache — shared across all Sfp instances.
# Reads 4 PCA9535 bytes per cache refresh instead of 32 individual i2cgets.
# Keys: (bus, addr, offset); values: (timestamp, raw_byte)
# ---------------------------------------------------------------------------

_CACHE_TTL = 1.0  # seconds; xcvrd polls every few seconds
_presence_cache = {}   # {(bus, addr, offset): (ts, raw_byte)}


def _read_presence_byte(bus, addr, offset):
    """Read one byte from a PCA9535 with a short-lived cache."""
    key = (bus, addr, offset)
    now = time.monotonic()
    cached = _presence_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        cmd = 'i2cget -f -y {} 0x{:02x} 0x{:02x}'.format(bus, addr, offset)
        raw = int(subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip(), 0)
        _presence_cache[key] = (now, raw)
        return raw
    except Exception:
        return None


def _bit_swap(value):
    """
    Swap even/odd bit pairs per ONL sfpi.c onlp_sfpi_reg_val_to_port_sequence().

    PCA9535 GPIO lines are wired in interleaved order relative to the
    front-panel QSFP port sequence.  This corrects the bit ordering so
    that bit N corresponds to the Nth port within the group.
    """
    result = 0
    for i in range(8):
        if i % 2 == 1:
            result |= (value & (1 << i)) >> 1
        else:
            result |= (value & (1 << i)) << 1
    return result


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
        """
        Return the sysfs path to this port's EEPROM binary file.

        If the optoe1 kernel device has not yet been instantiated for this
        bus (i.e., the sysfs file does not exist), register it now using
        the kernel's new_device interface.  The optoe1 driver is designed
        for QSFP-type modules and handles upper-page access automatically.

        Prerequisite: 'modprobe optoe' must have been run before this is
        called (done by accton_wedge100s_util.py install).
        """
        path = _EEPROM_PATH.format(self._bus)
        if not os.path.exists(path):
            try:
                with open(_NEW_DEVICE.format(self._bus), 'w') as f:
                    f.write('optoe1 0x50\n')
            except OSError:
                pass
        return path

    # ------------------------------------------------------------------
    # DeviceBase / SfpBase API
    # ------------------------------------------------------------------

    def get_name(self):
        return 'QSFP28 {}'.format(self._port + 1)

    def get_presence(self):
        """
        True when a QSFP28 module is physically inserted in this port.

        Reads the PCA9535 GPIO expander for the port's group and applies
        the even/odd bit-swap that corrects the hardware GPIO wiring order.
        The PCA9535 PRESENT# pin is active-low (0 = present).
        """
        port   = self._port
        group  = 0 if port < 16 else 1
        bus    = _PRESENCE_BUS[group]
        addr   = _PRESENCE_ADDR[group]
        # sfpi.c: ports 0-7 and 16-23 use offset 0; ports 8-15 and 24-31 use offset 1
        offset = 0 if (port < 8 or 16 <= port <= 23) else 1
        raw = _read_presence_byte(bus, addr, offset)
        if raw is None:
            return False
        swapped = _bit_swap(raw)
        return not bool(swapped & (1 << (port % 8)))

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
