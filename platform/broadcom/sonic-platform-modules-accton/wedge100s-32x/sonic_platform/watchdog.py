#!/usr/bin/env python3
"""
sonic_platform/watchdog.py — Watchdog stub for Accton Wedge 100S-32X.

The x86 COMe module has an iTCO_wdt device but the BIOS sets the
NO_REBOOT flag, so the kernel driver cannot create /dev/watchdog.
Hardware watchdog functionality on this platform is managed by the
OpenBMC ASPEED WDT, not by the main CPU.

This stub satisfies the SONiC watchdog API (watchdogutil, watchdog-
control.service) without attempting hardware access.
"""

from sonic_platform_base.watchdog_base import WatchdogBase


class Watchdog(WatchdogBase):
    """Watchdog stub for Accton Wedge 100S-32X.

    The x86 iTCO_wdt watchdog is disabled by BIOS (NO_REBOOT flag set).
    Hardware watchdog is managed by the OpenBMC AST2500, which is not
    directly controllable from the host CPU.  All methods return the
    appropriate "not armed" / "not supported" sentinel values.
    """

    def arm(self, seconds):
        """Arm the watchdog with a timeout.

        Args:
            seconds: Watchdog timeout in seconds (ignored).

        Returns:
            int: -1, indicating the watchdog cannot be armed on this platform.
        """
        return -1

    def disarm(self):
        """Disarm the watchdog.

        Returns:
            bool: True (no-op; watchdog is never armed on this platform).
        """
        return True

    def is_armed(self):
        """Return whether the watchdog is currently armed.

        Returns:
            bool: Always False — the host watchdog is disabled by BIOS.
        """
        return False

    def get_remaining_time(self):
        """Return remaining time before watchdog fires.

        Returns:
            int: -1, indicating the watchdog is not armed.
        """
        return -1
