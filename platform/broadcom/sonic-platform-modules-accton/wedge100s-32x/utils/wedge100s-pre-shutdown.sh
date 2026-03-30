#!/bin/bash
# Kill processes holding /var/log open so the loop device (loop1) can unmount
# cleanly at shutdown without the 2-3 min systemd timeout.
# Culprits: auditd, rsyslogd, Docker container procs bind-mounting /var/log.
echo "wedge100s-pre-shutdown: killing /var/log holders for clean loop1 unmount..."
/usr/bin/fuser -k -TERM /var/log 2>/dev/null || true
sleep 2
/usr/bin/fuser -k -KILL /var/log 2>/dev/null || true
/bin/umount /var/log 2>/dev/null || /bin/umount -l /var/log 2>/dev/null || true
echo "wedge100s-pre-shutdown: done"
