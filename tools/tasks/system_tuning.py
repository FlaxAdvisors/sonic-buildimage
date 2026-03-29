"""SystemTuningTask — persistent kernel and systemd tuning for SSH reliability.

These settings are lost on fresh image install (writable overlay is wiped)
so deploy.py must reapply them.  All changes are idempotent.

Tuning applied
--------------
1. /etc/sysctl.d/99-wedge100s-ssh.conf
   tcp_syn_retries=2   — reconnect after BCM IRQ storm in ≤7 s (not ≤127 s)
   tcp_retries2=5      — give up stalled established connections after ~6 s

2. /etc/systemd/system/networking.service.d/restart-ssh.conf
   Restarts sshd after every networking restart so sshd re-binds to the
   mgmt VRF socket (which is recreated when networking tears down the VRF).

NOTE: noop-renew dhclient hook was removed.  dhclient-enter-hooks are
sourced (not exec'd), so "exit 0" exits dhclient-script itself — it was
silently skipping route installation on every RENEW after a networking
restart, causing the mgmt VRF default route to disappear.

See tests/notes/BEWARE_IRQ.md §3 for the BCM IRQ / TCP backoff root cause.
"""
import textwrap
from .base import Change, ConfigTask

SYSCTL_PATH = "/etc/sysctl.d/99-wedge100s-ssh.conf"
SYSCTL_CONTENT = textwrap.dedent("""\
    # BCM IRQ storm mitigation — see tests/notes/BEWARE_IRQ.md §3
    # Reconnect after storm completes in ≤7 s (default: ≤127 s).
    net.ipv4.tcp_syn_retries=2
    # TCP gives up stalled established connection after ~6 s (default: ~924 s).
    # xcvrd DOM storms fire every 60 s; retries2=8 gave up at ~51 s — just as
    # the next storm hit, causing 60/120/180 s stall cascades.  Value 5 (~6 s)
    # ensures reconnect completes well before the next 60 s cycle.
    net.ipv4.tcp_retries2=5
""")

DHCP_HOOK_PATH = "/etc/dhcp/dhclient-enter-hooks.d/noop-renew"

SYSTEMD_DROPIN_PATH = "/etc/systemd/system/networking.service.d/restart-ssh.conf"
SYSTEMD_DROPIN_CONTENT = textwrap.dedent("""\
    [Service]
    ExecStartPre=-/bin/sh -c 'pkill -f "dhclient.*eth0" ; sleep 1 ; true'
    ExecStartPost=-/bin/systemctl restart ssh
""")


class SystemTuningTask(ConfigTask):

    def check(self) -> list:
        changes = []

        # 1. sysctl file
        out, _, rc = self.ssh.run(f"cat {SYSCTL_PATH} 2>/dev/null", timeout=10)
        if out.strip() != SYSCTL_CONTENT.strip():
            changes.append(Change(
                item="sysctl tcp_syn_retries/tcp_retries2",
                current=out.strip() or "missing",
                desired="tcp_syn_retries=2, tcp_retries2=5",
                cmd="_write_sysctl",
            ))

        # Remove the noop-renew hook if present on target — it was causing
        # route loss by exiting dhclient-script on RENEW via sourced "exit 0".
        out, _, rc = self.ssh.run(f"test -f {DHCP_HOOK_PATH} && echo exists", timeout=10)
        if "exists" in out:
            changes.append(Change(
                item="dhclient noop-renew hook (remove)",
                current="present",
                desired="absent",
                cmd="_remove_dhcp_hook",
            ))

        # 2. systemd drop-in: restart ssh after networking restarts
        # Disabled — only needed when mgmt VRF is active (sshd must re-bind to
        # VRF socket after networking tears it down).  MgmtVrfTask is not in
        # TASK_ORDER so this drop-in is a no-op and causes unnecessary churn.

        return changes

    def apply(self, changes: list) -> None:
        for change in changes:
            if change.cmd == "_write_sysctl":
                self._write_file(SYSCTL_PATH, SYSCTL_CONTENT)
                self.ssh.run("sudo sysctl --system -q 2>/dev/null || true", timeout=15)
                print("  [system_tuning] sysctl applied", flush=True)

            elif change.cmd == "_remove_dhcp_hook":
                self.ssh.run(f"sudo rm -f {DHCP_HOOK_PATH}", timeout=5)
                print("  [system_tuning] noop-renew hook removed", flush=True)



    def verify(self) -> bool:
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [system_tuning] FAIL: {c}")
            return False
        return True

    def _write_file(self, path: str, content: str) -> None:
        import base64
        encoded = base64.b64encode(content.encode()).decode()
        self.ssh.run(
            f"sudo mkdir -p $(dirname {path}) && "
            f"echo {encoded} | base64 -d | sudo tee {path} > /dev/null",
            timeout=15,
        )
