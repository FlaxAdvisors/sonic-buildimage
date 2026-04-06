#!/bin/sh
# clear_led_diag.sh — SONiC host-side trigger to reset LED diagnostic registers.
# Signals wedge100s-bmc-daemon to run clear_led_diag.sh on the BMC via its
# persistent SSH path.  Safe to run while daemons are up; daemon serializes
# all BMC access.
touch /run/wedge100s/clear_led_diag.set
