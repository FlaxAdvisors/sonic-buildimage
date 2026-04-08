Porting Guide: Accton/Broadcom Platform to SONiC
==================================================

This guide documents the process used to port the Accton Wedge 100S-32X
to SONiC.  It serves as a template for porting other Accton Broadcom
platforms.

Prerequisites
-------------

Before starting, gather:

1. **ONL source** for the platform (``packages/platforms/accton/x86-64/<platform>/``)
   — contains ``i2cdef.h``, ``sfpi.c``, ``thermali.c``, ``fani.c``, ``psui.c``,
   ``cpld.c``.  These are the authoritative hardware register maps.

2. **An existing similar platform** in SONiC — e.g., AS7712-32X is the
   closest relative of the Wedge 100S (same BCM56960, similar I2C topology).
   Use it as a template for file structure and SONiC API patterns.

3. **Target hardware** with console access.  Many decisions require
   hardware verification (bus numbers, register offsets, presence bitmasks).

Step 1: Build System Integration
---------------------------------

**1.1 Add the platform .deb target**

Create ``platform/broadcom/platform-modules-accton.mk`` (or add to the
existing one if porting another Accton platform):

.. code-block:: make

    SONIC_PLATFORM_ACCTON_WEDGE100S_32X = sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
    $(SONIC_PLATFORM_ACCTON_WEDGE100S_32X)_PLATFORM = broadcom
    $(SONIC_PLATFORM_ACCTON_WEDGE100S_32X)_MACHINE = x86_64-accton_wedge100s_32x-r0
    SONIC_DPKG_DEBS += $(SONIC_PLATFORM_ACCTON_WEDGE100S_32X)

**1.2 Add to one-image.mk**

.. code-block:: make

    # In one-image.mk _LAZY_INSTALLS list:
    sonic-platform-accton-wedge100s-32x_1.1_amd64.deb

**1.3 Installer platform file**

Create ``installer/platforms/x86_64-accton_wedge100s_32x-r0``:

.. code-block:: bash

    CONSOLE_PORT=0x3f8
    CONSOLE_DEV=0
    CONSOLE_SPEED=57600
    GRUB_SERIAL_COMMAND="serial --port=0x3f8 --speed=57600 --word=8 --parity=no --stop=1"

**1.4 ASIC vendor mapping**

Add to ``installer/platforms_asic``:

.. code-block:: text

    x86_64-accton_wedge100s_32x-r0=broadcom

Step 2: Device Directory
------------------------

Create ``device/accton/x86_64-accton_wedge100s_32x-r0/``.

Required files:

- ``platform.json`` — port list (32 ports × 100G QSFP28)
- ``port_config.ini`` — port name, lanes, alias, speed per port
- ``sai.profile`` — SAI profile: ``SAI_KEY_INIT_CONFIG_FILE``, ``SAI_INIT_CONFIG_FILE``
- ``<hwsku>/`` — per-SKU directory with ``config.bcm``, ``led_proc_init.soc``,
  ``port_config.ini`` symlink or copy

**BCM config** (``config.bcm``): Port breakout is controlled by
``portmap_N.0=lane:speed`` entries.  Use the ONL ``i2cdef.h`` to
determine the correct lane mapping.  Verify with ``bcmcmd 'ps'`` on the
running switch.

**LED SOC file** (``led_proc_init.soc``): Contains the LED processor
bytecode (``led N prog <hex bytes>``) and port order remap
(``CMIC_LEDUP0_PORT_ORDER_REMAP_*``).  Copy from ONL or an existing
platform with the same BCM variant.

Step 3: Understand the I2C Topology
------------------------------------

This is the most platform-specific step.  For each resource, determine:

- Which I2C bus and address it lives on
- Whether the bus requires mux traversal and what the mux address/channel is
- What register layout it uses

Sources of truth (in order of reliability):

1. ONL ``i2cdef.h`` — authoritative register map
2. Hardware schematic
3. ``i2cdetect -l`` + ``i2cdetect -y <bus>`` on ONL or another NOS
4. Kernel dmesg on boot with drivers loaded

**Wedge 100S-32X I2C summary** (as an example)::

    CP2112 USB-HID → i2c-1
      0x32  CPLD (accton_wedge100s_cpld driver)
      0x70-0x73  PCA9548 muxes → QSFP EEPROM buses (2-33)
      0x74  PCA9548 → presence (ch2=0x22, ch3=0x23), LP_MODE (ch0=0x20, ch1=0x21),
                       system EEPROM (ch6=0x50 AT24C64)

    BMC i2c-3  → TMP75 sensors 0x48-0x4c
    BMC i2c-7  → PSU PMBus (mux 0x70 → 0x59, 0x5a)
    BMC i2c-8  → fan board 0x33, TMP75 0x48-0x49

Step 4: Decide on I2C Architecture
------------------------------------

For platforms with a **shared I2C bus resource** (single USB-HID bridge,
iSMT with no arbitration, or single CP2112), use the daemon architecture:

- Write a C daemon that owns the I2C bus exclusively
- Have all platform Python code read from ``/run/<platform>/`` files
- This prevents mux state corruption from concurrent access

For platforms with **independent I2C buses per subsystem** (separate
iSMT, i801, GPIO-based buses), the standard kernel driver approach
(``i2c_mux_pca954x``, ``optoe``, ``at24``, ``lm75``) works without
a daemon.

Step 5: Platform Module Structure
-----------------------------------

The platform module (``sonic-platform-modules-accton/<platform>/``)
contains:

.. code-block:: text

    build/
        debian/                  # .deb packaging
        Makefile
    modules/                     # C kernel modules (CPLD driver, etc.)
    sonic_platform/              # Python SONiC platform API
        __init__.py
        platform.py              # Platform entry point
        chassis.py               # Chassis: SFP list, fan drawers, PSUs
        sfp.py                   # QSFP28 implementation
        fan.py                   # Fan + FanDrawer implementation
        psu.py                   # PSU implementation
        thermal.py               # Thermal sensor implementation
        eeprom.py                # System EEPROM (ONIE TlvInfo)
        component.py             # Firmware components (CPLD, BIOS)
        watchdog.py              # Watchdog stub or implementation
        bmc.py                   # BMC communication helper
        platform_smbus.py        # SMBus handle pool (if direct smbus needed)
    utils/                       # Daemons, init script, diagnostic tools
        accton_<platform>_util.py  # Platform init (install/clean/show/sff)
        <platform>-i2c-daemon.c    # I2C daemon (if using daemon architecture)
        <platform>-bmc-daemon.c    # BMC polling daemon
        <platform>-bmc-auth.c      # SSH key provisioning
    service/                     # systemd service and timer units
    flex-counter-daemon/         # (if breakout ports need counter workaround)

Step 6: Implement sonic_platform Classes
-----------------------------------------

Work through each class in dependency order:

**6.1 bmc.py** — BMC communication first, since thermal/fan/PSU depend on it

Determine the BMC communication path:

- **SSH over management network**: standard for most platforms
- **SSH over USB CDC-ECM**: used on Wedge 100S where no management port
- **Serial console**: last resort; slow and blocking

**6.2 thermal.py** — verify sensor locations from ONL ``thermali.c``

Match ONL's ``directory[]`` array (sysfs paths) and threshold values.
For BMC-side sensors, the daemon writes millidegrees C files.

**6.3 fan.py** — verify from ONL ``fani.c``

Check the ``fantray_present`` bitmask polarity (active-low or active-high)
and the fan RPM sysfs attribute naming.  The ``MAX_FAN_SPEED`` constant
should match ``fani.c``'s maximum RPM value.

**6.4 psu.py** — verify from ONL ``psui.c``

PSU presence is typically from a CPLD register.  PMBus telemetry requires
PMBus LINEAR11 decoding.  Verify VOUT_MODE before deciding whether to
read VOUT directly or compute it as POUT/IOUT.

**6.5 sfp.py** — verify presence bitmask from ONL ``sfpi.c``

The ONL ``sfpi.c`` XOR-1 interleave pattern (``port_index ^ 1``) is used
on many Accton platforms.  Verify PCA9535 INPUT register active polarity.

**6.6 eeprom.py** — ONIE TlvInfo format

The system EEPROM is at a fixed location in the mux tree.  Read it once
at daemon startup; cache in ``/run/<platform>/syseeprom``.

Step 7: Platform Init (accton_util.py)
---------------------------------------

The ``install`` command loads kernel modules and registers I2C devices:

.. code-block:: python

    kos = [
        'modprobe i2c_dev',
        'modprobe hid_cp2112',
        'modprobe <platform>_cpld',
        # Do NOT load i2c_mux_pca954x, at24, optoe if using daemon architecture
    ]

    mknod = [
        'echo <cpld_driver> 0x<addr> > /sys/bus/i2c/devices/i2c-1/new_device',
    ]

**Important**: If using the daemon architecture, do NOT register I2C
devices for resources owned by the daemon.  Registering ``i2c_mux_pca954x``
would cause the kernel to probe-write to all mux children (including QSFP
EEPROM at 0x50) on every ``modprobe``, corrupting EEPROM data.

Step 8: Write Tests
-------------------

Create a test suite under ``tests/`` using pytest.  Essential test stages:

- **Stage 01**: System EEPROM — read TlvInfo, check Product Name/MAC/Serial
- **Stage 02**: Thermal — verify all N sensors return non-None temperatures
- **Stage 03**: Fan — verify presence, RPM > 0, direction
- **Stage 04**: PSU — verify presence, power-good, voltage > 0
- **Stage 05**: SFP — verify get_presence() for all ports (no crash)
- **Stage 06**: Platform init — verify ``install``/``clean`` cycle

Run with: ``cd tests && python3 run_tests.py``

Step 9: Build and Install
--------------------------

.. code-block:: bash

    # Build the .deb
    BLDENV=trixie make target/debs/trixie/sonic-platform-accton-<platform>_1.1_amd64.deb

    # Install on target
    scp target/debs/trixie/sonic-platform-accton-<platform>*.deb admin@<target>:~
    ssh admin@<target> sudo systemctl stop pmon
    ssh admin@<target> sudo dpkg -i sonic-platform-accton-<platform>*.deb
    ssh admin@<target> sudo systemctl start pmon

Step 10: Common Pitfalls
-------------------------

**Mux state corruption**
    Always deselect muxes after use (write 0x00 to the mux I2C address).
    Leaving a mux selected causes the next I2C access on the same bus to
    go to the wrong child.

**PCA9535 presence bitmask polarity**
    ONL sometimes inverts the present/absent polarity in the register
    description vs the actual hardware.  Verify with ``i2cget`` while a
    module is inserted vs removed.

**EEPROM byte 220 (DIAG_MON_TYPE) quirk**
    Some QSFP28 modules (e.g., Arista QSFP28-SR4-100G) have bit 5 clear
    in byte 220, making ``Sff8636Api.get_temperature_support()`` return
    False even though bytes 22-23 contain valid temperature data.  Patch
    the API instance in ``get_xcvr_api()`` to force temperature support.

**glibc version mismatch in daemons**
    Daemons compiled on trixie (glibc 2.37) that run inside containers
    based on bookworm (glibc 2.36) may fail on ``__isoc23_sscanf``.
    Include a ``compat.c`` shim that aliases ``__isoc23_sscanf`` to
    ``__isoc99_sscanf``.

**LP_MODE deassert timing**
    After deasserting LP_MODE (allowing TX), wait at least 2.5 seconds
    before reading the EEPROM.  SFF-8636 module MCUs need time to
    initialize after the TX laser is enabled.  See ``LP_MODE_READY_NS``
    in ``wedge100s-i2c-daemon.c``.
