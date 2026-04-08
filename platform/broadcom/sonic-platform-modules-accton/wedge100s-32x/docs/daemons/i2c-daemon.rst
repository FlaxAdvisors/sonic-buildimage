wedge100s-i2c-daemon
=====================

Purpose
-------

``wedge100s-i2c-daemon`` is the sole owner of the CP2112 USB-HID bridge
(``/dev/hidraw0``) on the Wedge 100S-32X.  It is a one-shot C program
invoked every 3 seconds by ``wedge100s-i2c-poller.timer`` and exits after
each polling cycle.

The daemon handles all I2C access to the CP2112 mux tree, including:

- QSFP28 presence polling (PCA9535 chips)
- QSFP28 EEPROM caching (256 bytes of page 0 per port)
- LP_MODE control (PCA9535 GPIO lines)
- System EEPROM read (AT24C64, once at first boot)
- Status LED write (CPLD register 0x3e via sysfs)
- On-demand EEPROM read requests from pmon (DOM refresh)
- On-demand EEPROM write requests from pmon (xcvrd SFP control)

Design Rationale
----------------

The CP2112 USB-HID bridge is a shared single resource with no hardware
arbitration.  Running ``i2c_mux_pca954x``, ``optoe``, and ``at24`` as
kernel drivers alongside userspace I2C access from pmon caused mux state
corruption and QSFP EEPROM byte-corruption (bytes 0x00/0xFF patterns at
addresses 220–255).

The daemon solves this by:

1. Bypassing the kernel I2C stack entirely.  It communicates with the
   CP2112 via raw AN495 HID reports (``write()``/``read()`` on
   ``/dev/hidraw0``), implementing the full read/write/write-read transfer
   sequence including mux select/deselect and transfer status polling.

2. Being the **only** process that opens ``/dev/hidraw0``.  The kernel
   drivers ``i2c_mux_pca954x``, ``at24``, and ``optoe`` are not loaded.

3. Writing all results to ``/run/wedge100s/`` as plain files.  All
   consumers (``sonic_platform``, diagnostic tools) read these files.

Transport: Two Paths
--------------------

The daemon selects its I2C transport at startup:

**Phase 2 (preferred): hidraw direct**
    Opens ``/dev/hidraw0`` and communicates with the CP2112 via raw HID
    reports.  This is the default path on the production image.
    Uses HID report IDs from the Linux kernel ``hid-cp2112.c``::

        0x10  DATA_READ_REQUEST
        0x11  DATA_WRITE_READ_REQUEST  (write-then-read, repeated start)
        0x12  DATA_READ_FORCE_SEND
        0x13  DATA_READ_RESPONSE
        0x14  DATA_WRITE_REQUEST
        0x15  TRANSFER_STATUS_REQUEST
        0x16  TRANSFER_STATUS_RESPONSE
        0x17  CANCEL_TRANSFER

**Phase 1 (fallback): sysfs / i2c-dev**
    Used when ``/dev/hidraw0`` is unavailable.  Accesses PCA9535 via
    ``/dev/i2c-36`` and ``/dev/i2c-37`` using ``ioctl(I2C_RDWR)``;
    QSFP EEPROMs via optoe1 sysfs; system EEPROM via at24 sysfs.
    Requires ``hid_cp2112``, ``i2c_mux_pca954x``, ``optoe``, and ``at24``
    to be loaded.

Output Files
------------

All files are written atomically (write to ``.tmp`` then ``rename()``):

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - File
     - Content
   * - ``sfp_N_present``
     - ``"1"`` if QSFP28 module is inserted, ``"0"`` if absent (N = 0–31)
   * - ``sfp_N_eeprom``
     - 256 bytes of EEPROM page 0 (written on insertion only)
   * - ``sfp_N_lpmode``
     - ``"1"`` if LP_MODE is asserted (low-power), ``"0"`` if deasserted
   * - ``sfp_N_read_req``
     - Read request from pmon: JSON ``{offset, length}``
   * - ``sfp_N_read_resp``
     - Read response to pmon: hex-encoded bytes or ``"err:<msg>"``
   * - ``sfp_N_write_req``
     - Write request from pmon: JSON ``{offset, length, data_hex}``
   * - ``sfp_N_write_ack``
     - Write acknowledgement to pmon: ``"ok"`` or ``"err:<msg>"``
   * - ``sfp_N_lpmode_req``
     - LP_MODE request from pmon: ``"0"`` or ``"1"``
   * - ``syseeprom``
     - Raw 8192-byte ONIE TlvInfo EEPROM (written once at first boot)
   * - ``led_sys1``
     - System LED value written by pmon; daemon picks up and applies to CPLD

Build
-----

.. code-block:: bash

    gcc -O2 -o wedge100s-i2c-daemon utils/wedge100s-i2c-daemon.c

Systemd Integration
-------------------

.. code-block:: ini

    # wedge100s-i2c-poller.timer
    [Timer]
    OnActiveSec=0
    OnUnitActiveSec=3s

    # wedge100s-i2c-poller.service (one-shot, invoked by timer)
    [Service]
    Type=oneshot
    ExecStart=/usr/local/bin/wedge100s-i2c-daemon poll-presence

The timer fires immediately on first activation (``OnActiveSec=0``) and
then every 3 seconds.  The daemon exits after each cycle.  If a previous
invocation is still running when the timer fires, the timer skips that
cycle (``oneshot`` prevents concurrent runs).
