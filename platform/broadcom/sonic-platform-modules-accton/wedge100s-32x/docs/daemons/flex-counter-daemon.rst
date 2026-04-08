flex-counter-daemon
====================

Purpose
-------

``flex-counter-daemon`` provides hardware port counters for breakout
sub-ports on the Broadcom Tomahawk (BCM56960).

**Problem**: SONiC's standard FlexCounter calls SAI ``get_port_stats`` for
all ports.  On Tomahawk, ``get_port_stats`` fails for breakout sub-ports
that have fewer than 4 lanes (e.g., 4x25G or 4x10G breakout creates 4
sub-ports, each with 1 lane, while Tomahawk requires the parent port for
stats).  The result is that ``portstat`` shows no counters for breakout
ports.

**Solution**: ``flex-counter-daemon`` reads counters directly from the BCM
diagnostic shell via the ``bcmcmd`` Unix domain socket, bypassing SAI.
It handles the FlexCounter DB interlock (removing breakout ports from
``FLEX_COUNTER_TABLE`` to prevent SAI from attempting and failing) and
writes counter values directly to ``COUNTERS_DB``.

Design
------

The daemon runs on the host (not inside the syncd container) and connects
to the ``bcmcmd`` socket exposed by syncd at
``/var/run/docker-syncd/sswsyncd.socket``.

**Poll cycle (every 3 seconds)**:

1. Discover breakout ports from ``COUNTERS:LANES`` in ``COUNTERS_DB``.
2. Remove breakout ports from ``FLEX_COUNTER_TABLE`` (DB 5) if not
   already removed.
3. Fetch counters via ``show c all <port>`` for small port counts
   (< 64 ports) or ``show c all`` bulk query for larger counts.
4. Map BCM counter names to SAI stat field names using ``stat_map``.
5. Write counter values to ``COUNTERS:<oid>`` hashes in ``COUNTERS_DB``.
6. Compute EWMA-smoothed rates (alpha = 0.18, matching ``port_rates.lua``)
   and write to ``RATES:<oid>``.
7. Detect syncd restarts (OID changes in ``COUNTERS:LANES``) and re-run
   the interlock.

Counter Fetch Strategy
~~~~~~~~~~~~~~~~~~~~~~

Two modes, selected by port count:

- **Per-port** (< 64 breakout ports): ``show c all <port>`` for each port.
  Approximately 17 ms per port.  For 12 breakout ports, ~200 ms total.

- **Bulk** (≥ 64 breakout ports): ``show c all`` returns all 128 ports
  (~1.35 MB, ~2 seconds).  Used as a fallback when per-port would exceed
  the poll interval.

BCM Counter Mapping
~~~~~~~~~~~~~~~~~~~

The ``stat_map`` module maps SAI ``sai_port_stat_t`` enum values to BCM
counter names (from ``show c all`` output).  Some SAI stats are compound
(e.g., ``IF_IN_NON_UCAST_PKTS = RMCA + RBCA``) and require two BCM
counter names.  The mapping is defined in ``stat_map.c`` and mirrors
Broadcom's SAI implementation for Tomahawk.

glibc Compatibility
~~~~~~~~~~~~~~~~~~~

The daemon is compiled on Debian trixie (glibc 2.37) but runs inside the
syncd container based on Debian bookworm (glibc 2.36).  GCC 13+ compiles
``sscanf`` calls to ``__isoc23_sscanf`` (GLIBC_2.38), which is absent in
bookworm.  The ``compat.c`` shim provides ``__isoc23_sscanf`` as an alias
of ``__isoc99_sscanf`` to remove this runtime dependency.

Redis Connection
~~~~~~~~~~~~~~~~

The daemon uses ``hiredis`` with configurable I/O timeout (500 ms) to
prevent hangs during dynamic port breakout (DPB) operations.  If the Redis
connection is lost (e.g., during DPB reconfiguration), the daemon
auto-reconnects on the next poll cycle.

Build
-----

.. code-block:: bash

    make -C flex-counter-daemon

The ``Makefile`` links against ``hiredis`` and includes ``compat.c``:

.. code-block:: make

    flex-counter-daemon: daemon.c bcmcmd_client.c stat_map.c compat.c
        gcc -O2 -o $@ $^ -lhiredis

Systemd Integration
-------------------

.. code-block:: ini

    [Service]
    Type=simple
    ExecStart=/usr/local/bin/flex-counter-daemon
    Restart=on-failure
    RestartSec=5

The daemon starts after ``syncd.service`` (requires the bcmcmd socket to
be available) and before ``swss.service`` starts FlexCounter.

Configuration
-------------

The daemon auto-detects the BCM config file at startup by globbing:

- ``/usr/share/sonic/hwsku/*.config.bcm`` (inside syncd container)
- ``/usr/share/sonic/device/*/Accton-WEDGE100S*/*.config.bcm`` (host)

The config file provides the ``portmap_N.0=lane:speed`` entries used to
map physical lanes to SDK port numbers.
