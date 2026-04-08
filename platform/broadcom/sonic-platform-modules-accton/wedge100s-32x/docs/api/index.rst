Python API Reference
====================

The ``sonic_platform`` package implements the SONiC platform API for the
Wedge 100S-32X.  All classes consume data from ``/run/wedge100s/`` files
written by the platform daemons; no class performs direct I2C bus access.

.. toctree::
   :maxdepth: 1

   platform
   chassis
   sfp
   fan
   psu
   thermal
   bmc
   eeprom
   component
   watchdog
   platform_smbus
