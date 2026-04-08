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

    def arm(self, seconds):
        return -1

    def disarm(self):
        return True

    def is_armed(self):
        return False

    def get_remaining_time(self):
        return -1
