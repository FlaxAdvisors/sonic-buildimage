#!/bin/sh
# clear_led_diag_bmc.sh — disable syscpld LED test pattern, enable TH LEDUP output.
#
# Deployed to the BMC by SONiC platform-init (accton_wedge100s_util.py:_bmc_led_init())
# and run there — NOT on the SONiC host. The syscpld (i2c-12 addr 0x31) is
# physically accessible from the BMC, not from SONiC, so all register writes
# happen on the BMC side via its sysfs interface.
#
# Writes four bits in syscpld register 0x3c:
#   led_test_mode_en  = 0  (bit 7) — leave diag/sweep mode
#   led_test_blink_en = 0  (bit 6) — stop diag blink
#   walk_test_en      = 0  (bit 3) — disable LED walk test
#   th_led_en         = 1  (bit 1) — enable Tomahawk LEDUP output
#
# Safe to run at any time — writes are idempotent. Called once per boot from
# the SONiC side, plus on demand when /run/wedge100s/clear_led_diag.set is
# written on the SONiC host (wedge100s-bmc-daemon dispatches via SSH).
. /usr/local/bin/board-utils.sh
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_mode_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_blink_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/walk_test_en
echo 1 > ${SYSCPLD_SYSFS_DIR}/th_led_en
