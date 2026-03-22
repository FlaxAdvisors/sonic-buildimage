"""MgmtVrfTask — ensure management VRF is configured and SSH is in VRF.

Architecture note
-----------------
`config vrf add mgmt` only writes MGMT_VRF_CONFIG|vrf_global to config_db.
hostcfgd watches that key and runs `systemctl restart interfaces-config`, which
regenerates /etc/network/interfaces from a Jinja2 template and runs ifup.  ifup
creates the kernel VRF interface, masters eth0, and installs the routing table.
This is all asynchronous.

deploy.py must NOT manually run `ip link set eth0 master mgmt` or
`ip route add` — those are owned by interfaces-config.  The only root-cause
actions are:
  1. Write the SSH drop-in (so the restarted SSH binds inside the VRF).
  2. Run `config vrf add mgmt` (triggers the whole SONiC chain).
  3. Wait for eth0 to be mastered (interfaces-config done), then restart SSH.
"""
import time
from .base import Change, ConfigTask

SSH_DROP_IN_PATH = "/etc/systemd/system/ssh.service.d/sonic.conf"
SSH_EXEC_START   = "ExecStart=/usr/bin/ip vrf exec mgmt /usr/sbin/sshd -D $SSHD_OPTS"


class MgmtVrfTask(ConfigTask):

    # SONiC uses routing table 5000 for the management VRF.
    MGMT_VRF_TABLE = 5000

    def check(self) -> list:
        changes = []
        gw  = self.topology["device"]["mgmt_gateway"]
        vrf = self.topology["device"]["mgmt_vrf"]

        # 1. CONFIG_DB entry — root cause; hostcfgd does all kernel setup from this.
        redis_out, _, _ = self.ssh.run(
            "redis-cli -n 4 hget 'MGMT_VRF_CONFIG|vrf_global' mgmtVrfEnabled",
            timeout=10,
        )
        if redis_out.strip() != "true":
            changes.append(Change(
                item=f"VRF {vrf}",
                current=redis_out.strip() or "unset",
                desired="present",
                cmd=f"sudo config vrf add {vrf}",
            ))

        # 2. Derived: eth0 mastered into VRF (done by interfaces-config via hostcfgd).
        #    Listed for visibility; apply() does NOT issue this command manually.
        out, _, _ = self.ssh.run("ip link show eth0", timeout=10)
        if f"master {vrf}" not in out:
            changes.append(Change(
                item=f"eth0 master {vrf}",
                current="not in VRF",
                desired=f"master {vrf}",
                cmd="_derived",
            ))

        # 3. Derived: default route in mgmt routing table (set by ifup template).
        out, _, _ = self.ssh.run(
            f"ip route show table {self.MGMT_VRF_TABLE}", timeout=10,
        )
        if "default" not in out:
            changes.append(Change(
                item="mgmt default route",
                current="missing",
                desired=f"via {gw}",
                cmd="_derived",
            ))

        # 4. SSH drop-in — root cause; must exist before SSH restarts.
        _, _, rc = self.ssh.run(
            f"grep -qF '{SSH_EXEC_START}' {SSH_DROP_IN_PATH} 2>/dev/null",
            timeout=10,
        )
        if rc != 0:
            changes.append(Change(
                item="SSH VRF drop-in",
                current="missing or incorrect",
                desired="ExecStart in VRF",
                cmd="_write_ssh_dropin",
            ))

        return changes

    def apply(self, changes: list) -> None:
        vrf = self.topology["device"]["mgmt_vrf"]

        ssh_dropin_needed = any(c.cmd == "_write_ssh_dropin" for c in changes)
        vrf_config_needed = any(f"config vrf add {vrf}" in c.cmd for c in changes)

        # Step 1: Write SSH drop-in while the connection is stable.
        # The restarted SSH will pick this up and bind inside the VRF.
        if ssh_dropin_needed:
            self._write_ssh_dropin()

        if not vrf_config_needed:
            # Only the drop-in changed — reload + restart in-band, then reconnect.
            print("  [mgmt_vrf] Restarting SSH (applying drop-in)...", flush=True)
            self.ssh.run(
                "sudo systemctl daemon-reload && sudo systemctl restart ssh",
                timeout=15,
            )
            self._reconnect_ssh()
            return

        # Step 2: Build and push a self-contained setup script.
        #
        # `config vrf add mgmt` writes to config_db; hostcfgd picks it up and runs
        # `systemctl restart interfaces-config`, which creates the kernel VRF,
        # masters eth0, and sets up routing.  All of that is asynchronous, so we
        # poll until eth0 is mastered before restarting SSH.
        #
        # We do NOT include `ip link set eth0 master mgmt` or `ip route add` here —
        # interfaces-config owns those; issuing them manually races with ifup and
        # causes "Device does not exist" errors.
        log_path    = "/tmp/deploy_mgmt_vrf.log"
        script_path = "/tmp/deploy_mgmt_vrf.sh"
        script = "\n".join([
            "#!/bin/bash",
            f"exec > {log_path} 2>&1",
            "set -x",
            # Trigger SONiC VRF setup (hostcfgd → interfaces-config → ifup).
            f"sudo config vrf add {vrf}",
            # Wait up to 60 s for interfaces-config to master eth0.
            f"for i in $(seq 1 60); do",
            f"  ip link show eth0 2>/dev/null | grep -q 'master {vrf}' && break",
            f"  sleep 1",
            f"done",
            # Fallback: if interfaces-config didn't master eth0, do it explicitly.
            # SSH must not restart until eth0 is in the VRF — otherwise it binds
            # on the wrong namespace and becomes unreachable.
            f"ip link show eth0 | grep -q 'master {vrf}' || sudo ip link set eth0 master {vrf}",
            # Kill ALL dhclient processes for eth0 with SIGKILL — interfaces-config
            # will start a fresh VRF-aware one.  Use SIGKILL (not SIGTERM) because
            # dhclient with -1 flag may delay or ignore SIGTERM while handling a
            # renewal, leaving a stale default-namespace client that disrupts the
            # management interface on every subsequent DHCP renewal cycle.
            "pkill -9 -f 'dhclient.*eth0' || true",
            # Brief pause so interfaces-config can restart dhclient in the VRF
            # before SSH restarts (SSH restart is not dhclient-dependent, but
            # waiting avoids a race where the new dhclient and SSH both start at once).
            "sleep 1",
            # Reload systemd (picks up the drop-in written before this script ran),
            # then restart SSH so it listens inside the VRF.
            "systemctl daemon-reload",
            "systemctl restart ssh",
        ]) + "\n"

        sftp = self.ssh._client.open_sftp()
        try:
            with sftp.file(script_path, "w") as fh:
                fh.write(script)
        finally:
            sftp.close()

        self.ssh.run(f"chmod +x {script_path}", timeout=5)
        print("  [mgmt_vrf] Firing VRF setup script (SSH connection will drop)...",
              flush=True)
        try:
            self.ssh.run(f"nohup sudo bash {script_path} &", timeout=5)
        except Exception:
            pass  # Expected — eth0 moves into the VRF, dropping our connection.

        print("  [mgmt_vrf] Waiting for SSH to come back up inside VRF...", flush=True)
        self._reconnect_ssh()

        out, _, _ = self.ssh.run(f"cat {log_path} 2>/dev/null", timeout=10)
        if out.strip():
            print(f"  [mgmt_vrf] Script output:\n{out.rstrip()}")

    def verify(self) -> bool:
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [mgmt_vrf] FAIL: {c}")
            return False
        return True

    # ------------------------------------------------------------------ helpers

    def _write_ssh_dropin(self):
        # `ExecStart=` (empty line) clears the inherited value before overriding.
        # Without this, systemd rejects the unit for having multiple ExecStart
        # entries on a non-oneshot service.
        dropin_content = "[Service]\nExecStart=\n" + SSH_EXEC_START + "\n"
        self.ssh.run(
            "sudo mkdir -p /etc/systemd/system/ssh.service.d",
            timeout=10,
        )
        self.ssh.run(
            f"printf '%s' '{dropin_content}' | sudo tee {SSH_DROP_IN_PATH} > /dev/null",
            timeout=10,
        )

    def _reconnect_ssh(self):
        """Retry SSH every 5 s for up to 2 minutes.

        After connecting, verify eth0 is mastered into the VRF — this
        distinguishes the post-VRF SSH from the old pre-VRF server that may
        still be accepting connections while the background script runs.
        """
        vrf = self.topology["device"]["mgmt_vrf"]
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                self.ssh.connect(retries=1, retry_delay=0)
                # Confirm we're on the post-VRF SSH, not the old server
                out, _, _ = self.ssh.run("ip link show eth0", timeout=5)
                if f"master {vrf}" in out:
                    return
                # Connected to old SSH — close and keep waiting
                self.ssh.close()
            except Exception:
                pass
            time.sleep(5)
        raise SystemExit(
            "ERROR: SSH did not come up inside VRF after 2 minutes — "
            "check /tmp/deploy_mgmt_vrf.log on the switch and re-run deploy.py"
        )
