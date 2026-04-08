wedge100s-bmc-daemon
=====================

Purpose
-------

``wedge100s-bmc-daemon`` polls all BMC-side sensors and writes their
values to ``/run/wedge100s/`` so that the ``sonic_platform`` Python API
can serve pmon and thermalctld without SSH latency on every read.

The daemon runs every 10 seconds via ``wedge100s-bmc-poller.timer``.
It also handles write requests from platform code via inotify on ``.set``
files in ``/run/wedge100s/``.

Design
------

The BMC is connected to the host via two paths:

1. **USB CDC-ECM (primary)**: The BMC presents a USB gadget network
   interface (``usb0``) that creates an Ethernet link to the host.  Both
   ends auto-configure IPv6 link-local addresses from their MAC addresses:

   - BMC ``usb0`` MAC ``02:00:00:00:00:01`` → ``fe80::ff:fe00:1``
   - Host ``usb0`` MAC ``02:00:00:00:00:02`` → ``fe80::ff:fe00:2``

   SSH uses this link with the key ``/etc/sonic/wedge100s-bmc-key`` (ed25519).

2. **Serial console (provisioning only)**: ``/dev/ttyACM0`` at 57600 8N1.
   Used exclusively by ``wedge100s-bmc-auth`` to push the SSH public key
   on every connect, because the BMC clears ``authorized_keys`` on reboot.

SSH ControlMaster Design
~~~~~~~~~~~~~~~~~~~~~~~~

Each invocation of ``wedge100s-bmc-daemon``:

1. Calls ``wedge100s-bmc-auth`` to push the SSH key via TTY (fast: ~300 ms
   if already logged in).
2. Establishes an SSH ControlMaster session (``-f -N``, background).
3. Issues all sensor read commands as individual SSH connections that
   reuse the ControlMaster socket (one TLS handshake per cycle instead
   of one per command).
4. Checks socket liveness before each cycle; reconnects if dead.
5. On exit, issues ``ssh -O exit`` to tear down the ControlMaster.

This reduces per-cycle SSH overhead from ~N×200 ms (N commands × round
trip) to one handshake plus N×5 ms (multiplexed).

Write Request Dispatch
~~~~~~~~~~~~~~~~~~~~~~

Platform code writes to ``/run/wedge100s/<name>.set`` to request BMC
actions.  The daemon detects these via inotify (``IN_CLOSE_WRITE``) and
dispatches the appropriate BMC SSH command:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - ``.set`` file
     - Action
   * - ``led_ctrl_write.set``
     - Write value to CPLD 0x3c (LED control); read back; write result to ``cpld_led_ctrl``
   * - ``cpld_led_ctrl.set``
     - Read CPLD 0x3c; write result to ``cpld_led_ctrl``
   * - ``led_color_read.set``
     - Read CPLD 0x3d; write result to ``cpld_led_color``
   * - ``clear_led_diag.set``
     - Run ``/usr/local/bin/clear_led_diag.sh`` on BMC

Output Files
------------

All values are plain decimal integers in ``/run/wedge100s/``:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - File
     - Content
   * - ``thermal_1`` – ``thermal_7``
     - TMP75 temperature in millidegrees C (divide by 1000 for °C)
   * - ``fan_present``
     - Bitmask: bit N set = fan tray N+1 absent; 0x00 = all present
   * - ``fan_{1..5}_front``
     - Front rotor RPM for fan tray N
   * - ``fan_{1..5}_rear``
     - Rear rotor RPM for fan tray N
   * - ``psu_{1,2}_vin``
     - AC input voltage, raw PMBus LINEAR11 word (decimal)
   * - ``psu_{1,2}_iin``
     - AC input current, raw PMBus LINEAR11 word (decimal)
   * - ``psu_{1,2}_iout``
     - DC output current, raw PMBus LINEAR11 word (decimal)
   * - ``psu_{1,2}_pout``
     - DC output power, raw PMBus LINEAR11 word (decimal)
   * - ``qsfp_int``
     - BMC GPIO 31 value (0 = interrupt asserted)
   * - ``qsfp_led_position``
     - BMC GPIO 59 board strap (0 or 1, written once per reconnect)

Sensor Sources
--------------

Sources match ONL ``thermali.c``, ``fani.c``, and ``psui.c``:

- **Thermal**: BMC ``i2c-3`` (TMP75 at 0x48–0x4c) and ``i2c-8`` (0x48–0x49), sysfs hwmon
- **Fan**: BMC ``i2c-8``, fan-board controller at 0x33, sysfs
- **PSU**: BMC ``i2c-7``, PCA9546 mux at 0x70, PMBus at 0x59/0x5a

Build
-----

.. code-block:: bash

    gcc -O2 -o wedge100s-bmc-daemon utils/wedge100s-bmc-daemon.c
    gcc -O2 -o wedge100s-bmc-auth   utils/wedge100s-bmc-auth.c

Systemd Integration
-------------------

.. code-block:: ini

    # wedge100s-bmc-poller.timer
    [Timer]
    OnActiveSec=0
    OnUnitActiveSec=10s

    # wedge100s-bmc-poller.service (one-shot)
    [Service]
    Type=oneshot
    ExecStart=/usr/local/bin/wedge100s-bmc-daemon
