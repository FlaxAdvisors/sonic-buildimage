#!/bin/bash
# wedge100s-bus-reset.sh — Emergency CP2112 I2C bus recovery.
#
# Use when the i2c-daemon is stuck in a crash loop with
# "mux_deselect_all failed" or "PCA9535[N] mux select failed" messages.
#
# Steps:
#   1. Stop all platform daemons that touch the CP2112 bus.
#   2. Ensure usb0 (BMC CDC-Ethernet) is up so the BMC SSH path works.
#   3. Re-provision the BMC SSH key (cleared on every BMC reboot) — this
#      now runs wedge100s-bmc-auth over the network (IPv6 LL via usb0),
#      not the old /dev/ttyACM0 TTY path.
#   4. Hardware-reset the CP2112 chip via BMC SYSCPLD GPIO.
#   5. Hardware-reset the USB hub via BMC SYSCPLD GPIO.
#   6. Wait for the CP2112 to re-enumerate as /dev/hidraw0.
#   7. Re-up usb0 in case the USB authorize cycle (fallback in step 4/5)
#      or the BMC's own USB re-registration left cdc_ether DOWN.
#   8. Restart platform daemons.
#
# Usage: sudo wedge100s-bus-reset.sh
#
set -e

BMC_KEY="/etc/sonic/wedge100s-bmc-key"
BMC_HOST="root@fe80::ff:fe00:1%usb0"
SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5"

_up_usb0() {
    # Bring usb0 up if it exists.  Silent no-op if the interface hasn't
    # enumerated yet; caller is expected to tolerate that.
    if ip link show usb0 >/dev/null 2>&1; then
        ip link set usb0 up 2>/dev/null || true
    fi
}

echo "wedge100s-bus-reset: stopping platform daemons..."
systemctl stop wedge100s-i2c-daemon wedge100s-bmc-daemon pmon 2>/dev/null || true
sleep 1

echo "wedge100s-bus-reset: ensuring usb0 (BMC CDC-Ethernet) is up..."
_up_usb0

echo "wedge100s-bus-reset: re-provisioning BMC SSH key (network ssh-copy-id via usb0)..."
if /usr/bin/wedge100s-bmc-auth >/dev/null 2>&1; then
    echo "wedge100s-bus-reset: BMC SSH key provisioned"
else
    echo "wedge100s-bus-reset: WARNING: wedge100s-bmc-auth failed (usb0 not reachable?)" >&2
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

# BOTH the BMC-side reset_usb.sh AND the fallback USB-authorize cycle on
# the CP2112's USB hub cascade through cdc_ether, leaving usb0 DOWN after
# re-enumeration.  Without this re-up, subsequent BMC SSH escalations
# (bmc-daemon, bmc-auth) fail with "Network is unreachable" until the
# next full reboot.  See the 70-wedge100s-usb0-autoup.rules udev rule for
# the long-term fix — this is a belt-and-suspenders in case udev hasn't
# fired yet by the time we get here.
echo "wedge100s-bus-reset: re-upping usb0 (cdc_ether re-registers DOWN)..."
for i in $(seq 1 5); do
    if ip link show usb0 >/dev/null 2>&1; then
        ip link set usb0 up 2>/dev/null && \
            echo "wedge100s-bus-reset: usb0 UP" && break
    fi
    sleep 1
done

echo "wedge100s-bus-reset: restarting platform daemons..."
systemctl start wedge100s-i2c-daemon wedge100s-bmc-daemon
sleep 3
systemctl start pmon 2>/dev/null || true

echo "wedge100s-bus-reset: done. Check: sudo journalctl -u wedge100s-i2c-daemon -n 5"
