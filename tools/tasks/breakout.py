"""BreakoutTask — break out QSFP parent ports to sub-ports and configure them."""
import time
from .base import Change, ConfigTask


def _expected_subports(breakout_ports: list) -> list:
    """Expand breakout parent → list of expected sub-port names.

    After 4x breakout of EthernetN, SONiC creates EthernetN, EthernetN+1,
    EthernetN+2, EthernetN+3.
    """
    subports = []
    for bp in breakout_ports:
        parent = bp["parent"]
        base = int(parent.replace("Ethernet", ""))
        subports.extend([f"Ethernet{base + i}" for i in range(4)])
    return subports


class BreakoutTask(ConfigTask):

    def check(self) -> list:
        changes = []

        # 1. Mode changes
        for bp in self.topology["breakout_ports"]:
            parent = bp["parent"]
            desired_mode = bp["mode"]
            out, _, _ = self.ssh.run(
                f"redis-cli -n 4 hget 'BREAKOUT_CFG|{parent}' brkout_mode",
                timeout=10,
            )
            current_mode = out.strip()
            if current_mode != desired_mode:
                changes.append(Change(
                    item=f"breakout {parent}",
                    current=current_mode or "unset",
                    desired=desired_mode,
                    cmd=f"sudo config interface breakout {parent} '{desired_mode}' -y -f",
                ))

        # 2. Per-subport config (only for ports already in CONFIG_DB)
        for bp in self.topology["breakout_ports"]:
            sc = bp.get("subport_config", {})
            if not sc:
                continue
            parent = bp["parent"]
            base = int(parent.replace("Ethernet", ""))
            subports = [f"Ethernet{base + i}" for i in range(4)]

            for port in subports:
                # Skip if port doesn't exist in CONFIG_DB yet
                exists, _, _ = self.ssh.run(
                    f"redis-cli -n 4 exists 'PORT|{port}'", timeout=10
                )
                if exists.strip() != "1":
                    continue

                if "admin_status" in sc:
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
                    )
                    current = out.strip() or "down"
                    desired = sc["admin_status"]
                    if current != desired:
                        verb = "startup" if desired == "up" else "shutdown"
                        changes.append(Change(
                            item=f"{port} admin_status",
                            current=current,
                            desired=desired,
                            cmd=f"sudo config interface {verb} {port}",
                        ))

                if "speed" in sc:
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'PORT|{port}' speed", timeout=10
                    )
                    current = out.strip()
                    desired = sc["speed"]
                    if current != desired:
                        changes.append(Change(
                            item=f"{port} speed",
                            current=current or "unset",
                            desired=desired,
                            cmd=f"sudo config interface speed {port} {desired}",
                        ))

                if "fec" in sc:
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=10
                    )
                    current = out.strip()
                    desired = sc["fec"]
                    if current != desired:
                        changes.append(Change(
                            item=f"{port} fec",
                            current=current or "unset",
                            desired=desired,
                            cmd=f"sudo config interface fec {port} {desired}",
                        ))

        return changes

    def apply(self, changes: list) -> None:
        # Apply mode changes first so sub-ports exist before config changes
        mode_changes = [c for c in changes if c.item.startswith("breakout ")]
        config_changes = [c for c in changes if not c.item.startswith("breakout ")]

        for change in mode_changes:
            out, err, rc = self.ssh.run(change.cmd, timeout=60)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

        if mode_changes:
            # Wait for ALL expected sub-ports to appear in COUNTERS_PORT_NAME_MAP
            expected = _expected_subports(self.topology["breakout_ports"])
            deadline = time.time() + 120
            while time.time() < deadline:
                out, _, _ = self.ssh.run(
                    "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
                )
                present = set(out.split())
                if all(p in present for p in expected):
                    break
                time.sleep(3)

            # Re-run check() to pick up sub-ports that DPB created after the
            # initial check().  Those ports weren't in config_changes because
            # they didn't exist in CONFIG_DB when deploy started.
            fresh = [c for c in self.check() if not c.item.startswith("breakout ")]
            config_changes = fresh

        for change in config_changes:
            out, err, rc = self.ssh.run(change.cmd, timeout=30)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

    def verify(self) -> bool:
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [breakout] FAIL: {c}")
            return False
        return True
