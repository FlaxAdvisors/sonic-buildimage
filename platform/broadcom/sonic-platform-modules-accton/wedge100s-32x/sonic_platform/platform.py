#!/usr/bin/env python3
"""
sonic_platform/platform.py — Platform entry point for Accton Wedge 100S-32X.
"""

try:
    from sonic_platform_base.platform_base import PlatformBase
except ImportError as e:
    raise ImportError(str(e) + " - required module not found")

from sonic_platform.chassis import Chassis


class Platform(PlatformBase):
    """Platform-specific Platform class for Accton Wedge 100S-32X."""

    def __init__(self):
        PlatformBase.__init__(self)
        self._chassis = Chassis()
