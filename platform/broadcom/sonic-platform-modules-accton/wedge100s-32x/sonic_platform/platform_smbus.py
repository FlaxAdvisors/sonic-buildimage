#!/usr/bin/env python3
"""
sonic_platform/platform_smbus.py — Thread-safe SMBus handle pool.

Opens each I2C bus file descriptor once and keeps it open for the
lifetime of the process.  Eliminates the repeated fd open/close and
USB HID setup/teardown overhead on the CP2112 bridge that occurs when
smbus2.SMBus() is constructed per-call.

All platform modules that need direct smbus2 access should use
read_byte() from this module rather than creating their own SMBus
instances.  Uses force=True by default so kernel-driver-bound devices
(gpio-pca953x on 36-0022/37-0023, accton-cpld on 1/0x32) can be
read without unbinding the driver.

Thread safety: a single threading.Lock() serialises all bus operations.
I2C reads are fast (~0.5 ms) so lock hold time is negligible.
"""

import threading

try:
    import smbus2
    _SMBUS2_AVAILABLE = True
except ImportError:
    _SMBUS2_AVAILABLE = False

_pool = {}          # bus_num -> smbus2.SMBus | None (None = failed to open)
_lock = threading.Lock()


def _ensure_bus(bus_num):
    """Open bus if not already attempted.  Called with _lock held."""
    if bus_num not in _pool:
        if not _SMBUS2_AVAILABLE:
            _pool[bus_num] = None
        else:
            try:
                _pool[bus_num] = smbus2.SMBus(bus_num)
            except OSError:
                _pool[bus_num] = None
    return _pool[bus_num]


def read_byte(bus_num, addr, reg, force=True):
    """
    Read a single byte from an I2C device register.

    Args:
        bus_num: I2C bus number (/dev/i2c-N)
        addr:    7-bit I2C device address
        reg:     register (command) byte
        force:   use I2C_SLAVE_FORCE (default True — allows access to
                 devices already bound to a kernel driver)

    Returns int (0-255) on success, None on I2C error or unavailable bus.
    """
    with _lock:
        bus = _ensure_bus(bus_num)
        if bus is None:
            return None
        try:
            return bus.read_byte_data(addr, reg, force=force)
        except OSError:
            return None


def read_word(bus_num, addr, reg, force=True):
    """
    Read a 16-bit word from an I2C device register (little-endian SMBus word).

    Returns int (0-65535) on success, None on error.
    """
    with _lock:
        bus = _ensure_bus(bus_num)
        if bus is None:
            return None
        try:
            return bus.read_word_data(addr, reg, force=force)
        except OSError:
            return None
