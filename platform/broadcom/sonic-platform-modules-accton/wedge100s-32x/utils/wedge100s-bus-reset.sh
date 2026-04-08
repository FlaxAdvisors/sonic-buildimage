#!/bin/bash
# wedge100s-bus-reset.sh — Emergency CP2112 I2C bus recovery.
#
# Use when the i2c-daemon is stuck in a crash loop with
# "mux_deselect_all failed" or "PCA9535[N] mux select failed" messages.
#
# Steps:
#   1. Stop all platform daemons that touch the CP2112 bus.
#   2. Re-provision the BMC SSH key (cleared on every BMC reboot).
#   3. Hardware-reset the CP2112 chip via BMC SYSCPLD GPIO.
#   4. Hardware-reset the USB hub via BMC SYSCPLD GPIO.
#   5. Wait for the CP2112 to re-enumerate as /dev/hidraw0.
#   6. Restart platform daemons.
#
# Usage: sudo wedge100s-bus-reset.sh
#
set -e

BMC_KEY="/etc/sonic/wedge100s-bmc-key"
BMC_HOST="root@fe80::ff:fe00:1%usb0"
SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5"

echo "wedge100s-bus-reset: stopping platform daemons..."
systemctl stop wedge100s-i2c-daemon wedge100s-bmc-daemon pmon 2>/dev/null || true
sleep 1

echo "wedge100s-bus-reset: re-provisioning BMC SSH key via /dev/ttyACM0..."
if /usr/bin/wedge100s-bmc-auth >/dev/null 2>&1; then
    echo "wedge100s-bus-reset: BMC SSH key provisioned"
else
    echo "wedge100s-bus-reset: WARNING: wedge100s-bmc-auth failed (ttyACM0 unavailable?)" >&2
fi

echo "wedge100s-bus-reset: hardware-resetting CP2112 and USB hub via BMC..."
if ssh $SSH_OPTS -i "$BMC_KEY" "$BMC_HOST" \
        '/usr/local/bin/reset_cp2112.sh >/dev/null 2>&1 && sleep 1 && /usr/local/bin/reset_usb.sh >/dev/null 2>&1'; then
    echo "wedge100s-bus-reset: BMC reset complete"
else
    echo "wedge100s-bus-reset: WARNING: BMC SSH reset failed — trying USB authorize cycle..." >&2
    for d in /sys/bus/usb/devices/*/idVendor; do
        if grep -q "10c4" "$d" 2>/dev/null; then
            dev=$(dirname "$d")
            echo "0" > "$dev/authorized" && sleep 1 && echo "1" > "$dev/authorized"
            echo "wedge100s-bus-reset: CP2112 at $dev re-authorized"
        fi
    done
fi

echo "wedge100s-bus-reset: waiting for /dev/hidraw0 re-enumeration..."
for i in $(seq 1 10); do
    if [ -e /dev/hidraw0 ]; then
        echo "wedge100s-bus-reset: /dev/hidraw0 is back (${i}s)"
        break
    fi
    sleep 1
    if [ "$i" -eq 10 ]; then
        echo "wedge100s-bus-reset: ERROR: /dev/hidraw0 did not reappear after 10s" >&2
        exit 1
    fi
done

echo "wedge100s-bus-reset: restarting platform daemons..."
systemctl start wedge100s-i2c-daemon wedge100s-bmc-daemon
sleep 3
systemctl start pmon 2>/dev/null || true

echo "wedge100s-bus-reset: done. Check: sudo journalctl -u wedge100s-i2c-daemon -n 5"
