Platform Architecture
======================

Hardware Overview
-----------------

The Accton Wedge 100S-32X (also known as the Facebook Wedge 100S) is a
1U top-of-rack switch with the following key components:

- **CPU**: Intel Broadwell-DE D1508 (2-core, 2 GHz) on a COMe module
- **ASIC**: Broadcom Tomahawk BCM56960, 3.2 Tbps switching capacity
- **Ports**: 32 x QSFP28 (100G), all supporting breakout to 4x25G or 4x10G
- **Memory**: 16 GB DDR4 ECC
- **Storage**: 32 GB eMMC + M.2 SSD slot
- **BMC**: Aspeed AST2500 running OpenBMC (accessible via USB CDC-ECM and UART)
- **Fan trays**: 5 hot-swappable fan trays, each with front and rear rotors
- **PSUs**: 2 hot-swappable AC PSUs (Delta DPS-1100AB-6 A, 650 W rated)
- **System EEPROM**: AT24C64 (8 KiB) in ONIE TlvInfo format

I2C Topology
------------

The host CPU accesses I2C through a Silicon Labs CP2112 USB-HID bridge
(``/dev/hidraw0`` on the host, ``/dev/i2c-1`` via the ``hid_cp2112`` kernel
driver).  Behind the CP2112, a PCA9548 mux tree fans out to QSFP EEPROMs,
presence chips, and the system EEPROM::

    CP2112 (USB HID, /dev/hidraw0)
      └─ i2c-1  [0x32 CPLD, accton_wedge100s_cpld driver]
           │
           ├─ PCA9548 0x70  ch0-7  → buses  2-9   (QSFP ports  1-8 EEPROMs)
           ├─ PCA9548 0x71  ch0-7  → buses 10-17  (QSFP ports  9-16 EEPROMs)
           ├─ PCA9548 0x72  ch0-7  → buses 18-25  (QSFP ports 17-24 EEPROMs)
           ├─ PCA9548 0x73  ch0-7  → buses 26-33  (QSFP ports 25-32 EEPROMs)
           └─ PCA9548 0x74
                ├─ ch0 → 0x20 PCA9535 LP_MODE group A (ports 0-15)
                ├─ ch1 → 0x21 PCA9535 LP_MODE group B (ports 16-31)
                ├─ ch2 → 0x22 PCA9535 presence A      (ports 0-15)
                ├─ ch3 → 0x23 PCA9535 presence B      (ports 16-31)
                └─ ch6 → 0x50 AT24C64  system EEPROM

The QSFP-to-bus mapping follows the ONL ``sfpi.c`` interleave pattern
(odd/even pairs swapped): port 1 uses bus 3, port 2 uses bus 2, port 3
uses bus 5, etc.

The BMC has its own separate I2C buses (not accessible from the host):

- ``i2c-3``: TMP75 sensors 0x48–0x4c (mainboard)
- ``i2c-7``: PSU PMBus controller (PCA9546 mux at 0x70, PMBus at 0x59/0x5a)
- ``i2c-8``: Fan-board controller at 0x33, TMP75 sensors 0x48–0x49
- ``i2c-12``: CPLD at 0x31 (LED control registers 0x3c, 0x3d)

Design Choice: Userspace Daemon Architecture
---------------------------------------------

The Wedge 100S-32X SONiC port uses a userspace daemon architecture rather
than standard kernel I2C drivers.  The key constraint is the CP2112 USB-HID
bridge: it is a single shared resource with no hardware arbitration.
Concurrent access from multiple kernel drivers (``i2c_mux_pca954x``,
``optoe``, ``at24``) and userspace tools causes mux state corruption and
EEPROM data corruption.

The chosen solution:

- ``wedge100s-i2c-daemon`` is the **sole owner** of ``/dev/hidraw0``.  It
  accesses the CP2112 via raw AN495 HID reports, bypassing the kernel I2C
  stack entirely.  The kernel drivers ``i2c_mux_pca954x``, ``optoe``, and
  ``at24`` are **not loaded** on this platform.

- All platform consumers (``sonic_platform``, ``pmon``, diagnostic tools) read
  data from ``/run/wedge100s/`` files written by the daemon.  No Python code
  or pmon service ever touches ``/dev/hidraw0`` or ``/dev/i2c-1`` directly.

- The ``wedge100s-bmc-daemon`` communicates with the BMC over SSH via the
  USB CDC-ECM link (``usb0``, IPv6 link-local ``fe80::ff:fe00:1%usb0``).
  It writes sensor data to the same ``/run/wedge100s/`` directory.

This design provides a clean serialization boundary with no kernel
concurrency issues, and keeps the hot path (pmon polling) entirely in
userspace file reads.

Daemon Architecture
-------------------

Four daemons service the platform at runtime:

**wedge100s-i2c-daemon** (``utils/wedge100s-i2c-daemon.c``)
    Runs every 3 seconds (``wedge100s-i2c-poller.timer``).  Reads QSFP
    presence from PCA9535 chips, caches EEPROM data for inserted modules,
    manages LP_MODE via PCA9535 GPIO, and reads the system EEPROM once.
    Writes: ``/run/wedge100s/sfp_N_{present,eeprom,lpmode}``,
    ``/run/wedge100s/syseeprom``, ``/run/wedge100s/led_sys1``.

**wedge100s-bmc-daemon** (``utils/wedge100s-bmc-daemon.c``)
    Runs every 10 seconds (``wedge100s-bmc-poller.timer``).  Establishes an
    SSH ControlMaster session to the BMC, reads all thermal sensors, fan
    RPM, PSU PMBus telemetry, and GPIO state via multiplexed SSH commands.
    Writes: ``/run/wedge100s/thermal_{1..7}``,
    ``/run/wedge100s/fan_{1..5}_{front,rear}``,
    ``/run/wedge100s/psu_{1,2}_{vin,iin,iout,pout}``.
    Also handles inotify write-requests (CPLD LED register writes via BMC).

**wedge100s-bmc-auth** (``utils/wedge100s-bmc-auth.c``)
    Runs at every ``wedge100s-bmc-daemon`` reconnect.  Pushes the platform
    SSH public key to the BMC via the serial console (``/dev/ttyACM0``,
    57600 8N1) so the ControlMaster session can authenticate.  This is
    necessary because the BMC clears ``authorized_keys`` on every reboot.

**flex-counter-daemon** (``flex-counter-daemon/daemon.c``)
    Polls BCM hardware counters every 3 seconds via the ``bcmcmd`` Unix
    domain socket at ``/var/run/docker-syncd/sswsyncd.socket``.  Replaces
    SONiC's FlexCounter for breakout sub-ports (fewer than 4 lanes) where
    SAI ``get_port_stats`` fails on Tomahawk.  Computes EWMA-smoothed rates
    matching ``port_rates.lua`` behavior and writes results to
    ``COUNTERS_DB`` (Redis DB 2).

Service Dependency Graph
-------------------------

::

    platform-init.service
      └─ wedge100s-i2c-poller.timer  (every 3s)
           └─ wedge100s-i2c-daemon (one-shot)
      └─ wedge100s-bmc-poller.timer  (every 10s)
           └─ wedge100s-bmc-daemon (one-shot)
                └─ wedge100s-bmc-auth (on reconnect)

    pmon.service
      └─ reads /run/wedge100s/ (no I2C access)

    syncd.service
      └─ flex-counter-daemon.service (socket client to sswsyncd.socket)

Data Flow
---------

::

    Hardware Sensors
         │
         ├── QSFP EEPROM / presence / LP_MODE
         │       (CP2112 HID → wedge100s-i2c-daemon)
         │       → /run/wedge100s/sfp_N_{present,eeprom,lpmode}
         │
         ├── System EEPROM (AT24C64)
         │       (CP2112 HID → wedge100s-i2c-daemon)
         │       → /run/wedge100s/syseeprom
         │
         ├── TMP75 sensors, fan RPM, PSU PMBus
         │       (BMC SSH → wedge100s-bmc-daemon)
         │       → /run/wedge100s/thermal_N, fan_N_*, psu_N_*
         │
         └── BCM56960 hardware counters
                 (bcmcmd socket → flex-counter-daemon)
                 → Redis COUNTERS_DB (DB 2)

    /run/wedge100s/ files
         │
         └── sonic_platform Python API
               (psu.py, thermal.py, fan.py, sfp.py, chassis.py, eeprom.py)
               └── SONiC services: xcvrd, psud, thermalctld, syseepromd, ledd

    Redis COUNTERS_DB
         └── portstat, SONiC CLI, monitoring agents
