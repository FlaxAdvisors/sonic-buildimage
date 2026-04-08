Platform Daemons
================

The Wedge 100S-32X uses three C daemons to mediate all hardware access.
Python platform code never touches hardware directly; it only reads files
written by these daemons from ``/run/wedge100s/``.

.. toctree::
   :maxdepth: 1

   i2c-daemon
   bmc-daemon
   flex-counter-daemon
