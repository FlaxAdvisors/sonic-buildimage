#!/usr/bin/env python3
"""
sonic_platform/sfp.py — QSFP28 implementation for Accton Wedge 100S-32X.

All 32 ports are QSFP28 (100G).  Hardware access:

Presence:
  PCA9535 GPIO expanders registered as kernel gpiochips:
    i2c-36/0x22 (gpiochip label "36-0022"): ports 0-15  (mux 0x74 ch2)
    i2c-37/0x23 (gpiochip label "37-0023"): ports 16-31 (mux 0x74 ch3)
  Read via GPIO sysfs (/sys/class/gpio/gpioN/value) — the kernel
  gpio-pca953x driver handles I2C locking, eliminating bus contention
  from the old i2cget -f -y approach.
  GPIO wiring is interleaved even/odd (XOR-1 corrects port→pin mapping).
  Sysfs value 0 = module present (active low).

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
# PCA9535 GPIO presence — kernel sysfs interface
# ---------------------------------------------------------------------------

# PCA9535 I2C labels used by the kernel gpio-pca953x driver.
# These match the "{bus}-{addr:04x}" format in /sys/class/gpio/gpiochip*/label.
_GPIO_LABELS = ['36-0022', '37-0023']   # group 0 (ports 0-15), group 1 (ports 16-31)

_GPIO_SYSFS = '/sys/class/gpio'

# Populated lazily: port -> sysfs value path
_gpio_value_paths = {}  # {port: '/sys/class/gpio/gpioN/value'}
_gpio_bases = None       # [base0, base1] discovered from gpiochip labels


def _discover_gpio_bases():
    """Find GPIO chip bases from PCA9535 kernel driver labels."""
    global _gpio_bases
    if _gpio_bases is not None:
        return _gpio_bases
    bases = [None, None]
    try:
        for entry in os.listdir(_GPIO_SYSFS):
            if not entry.startswith('gpiochip'):
                continue
            label_path = os.path.join(_GPIO_SYSFS, entry, 'label')
            try:
                with open(label_path) as f:
                    label = f.read().strip()
            except OSError:
                continue
            for idx, expected in enumerate(_GPIO_LABELS):
                if label == expected:
                    base_path = os.path.join(_GPIO_SYSFS, entry, 'base')
                    with open(base_path) as f:
                        bases[idx] = int(f.read().strip())
    except OSError:
        pass
    _gpio_bases = bases
    return bases


def _port_gpio_number(port):
    """
    Return the Linux GPIO number for a port's PRESENT# pin.

    PCA9535 GPIO lines are wired in interleaved even/odd order (per ONL
    sfpi.c onlp_sfpi_reg_val_to_port_sequence).  The XOR-1 on the
    intra-chip offset corrects this: port 0→GPIO offset 1, port 1→0,
    port 2→3, port 3→2, etc.
    """
    bases = _discover_gpio_bases()
    group = port // 16
    base = bases[group]
    if base is None:
        return None
    return base + ((port % 16) ^ 1)


def _get_presence_path(port):
    """Return the sysfs value path for a port, exporting the GPIO if needed."""
    path = _gpio_value_paths.get(port)
    if path is not None:
        return path
    gpio = _port_gpio_number(port)
    if gpio is None:
        return None
    value_path = '{}/gpio{}/value'.format(_GPIO_SYSFS, gpio)
    if not os.path.exists(value_path):
        try:
            with open('{}/export'.format(_GPIO_SYSFS), 'w') as f:
                f.write(str(gpio))
        except OSError:
            return None
    _gpio_value_paths[port] = value_path
    return value_path


# ---------------------------------------------------------------------------
# Sysfs paths for EEPROM device instantiation
# ---------------------------------------------------------------------------

_EEPROM_PATH = '/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom'
_NEW_DEVICE  = '/sys/bus/i2c/devices/i2c-{}/new_device'


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

        Reads the PCA9535 PRESENT# GPIO via kernel sysfs.  The kernel
        gpio-pca953x driver handles I2C locking properly, eliminating
        the bus contention caused by the old i2cget -f -y approach.
        Active-low: sysfs value 0 = module present.
        """
        path = _get_presence_path(self._port)
        if path is None:
            return False
        try:
            with open(path) as f:
                return f.read().strip() == '0'
        except OSError:
            return False

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
