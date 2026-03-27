#!/bin/sh
# clear_led_diag.sh — disable syscpld LED test pattern, enable TH LEDUP output.
# BMC-side utility. Installed to BMC /usr/local/bin/ by SONiC platform-init.
# Safe to run at any time; idempotent.
# DO NOT run on the SONiC host — this script requires BMC sysfs paths.
. /usr/local/bin/board-utils.sh
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_mode_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/led_test_blink_en
echo 0 > ${SYSCPLD_SYSFS_DIR}/walk_test_en
echo 1 > ${SYSCPLD_SYSFS_DIR}/th_led_en
