"""BreakoutTask — break out QSFP parent ports to sub-ports and configure them."""
import time
from .base import Change, ConfigTask


def _subport_offsets(mode: str) -> list:
    """Return list of lane offsets for a breakout mode.

    4x modes (4x25G, 4x10G, etc.) → [0, 1, 2, 3]  (4 ports, 1 lane each)
    2x modes (2x50G, etc.)         → [0, 2]          (2 ports, 2 lanes each)
    1x modes (1x100G, default)     → [0]              (single port)
    """
    if mode.startswith("4x"):
        return [0, 1, 2, 3]
    if mode.startswith("2x"):
        return [0, 2]
    return [0]


def _expected_subports(breakout_ports: list) -> list:
    """Expand breakout parent → list of expected sub-port names."""
    subports = []
    for bp in breakout_ports:
        parent = bp["parent"]
        base = int(parent.replace("Ethernet", ""))
        offsets = _subport_offsets(bp["mode"])
        subports.extend([f"Ethernet{base + i}" for i in offsets])
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
                timeout=30,
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
            offsets = _subport_offsets(bp["mode"])
            subports = [f"Ethernet{base + i}" for i in offsets]

            for port in subports:
                # Skip if port doesn't exist in CONFIG_DB yet
                exists, _, _ = self.ssh.run(
                    f"redis-cli -n 4 exists 'PORT|{port}'", timeout=30
                )
                if exists.strip() != "1":
                    continue

                if "admin_status" in sc:
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=30
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
                        f"redis-cli -n 4 hget 'PORT|{port}' speed", timeout=30
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
                        f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=30
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

                if "autoneg" in sc:
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'PORT|{port}' autoneg", timeout=30
                    )
                    current = out.strip()
                    desired = sc["autoneg"]
                    if current != desired:
                        an_mode = "enabled" if desired == "on" else "disabled"
                        changes.append(Change(
                            item=f"{port} autoneg",
                            current=current or "unset",
                            desired=desired,
                            cmd=f"sudo config interface autoneg {port} {an_mode}",
                        ))

                if "adv_speeds" in sc:
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'PORT|{port}' adv_speeds", timeout=30
                    )
                    current = out.strip()
                    desired = sc["adv_speeds"]
                    if current != desired:
                        changes.append(Change(
                            item=f"{port} adv_speeds",
                            current=current or "unset",
                            desired=desired,
                            cmd=f"sudo config interface advertised-speeds {port} {desired}",
                        ))

        return changes

    def apply(self, changes: list) -> None:
        # Apply mode changes first so sub-ports exist before config changes
        mode_changes = [c for c in changes if c.item.startswith("breakout ")]
        config_changes = [c for c in changes if not c.item.startswith("breakout ")]

        # Seed BREAKOUT_CFG if absent — port_breakout_config_db.json only has PORT
        # entries; 'config interface breakout' hard-aborts without this table.
        # Read hwsku.json (has default_brkout_mode per parent port) and load via
        # redis-cli pipeline — faster and more reliable than sonic-cfggen.
        if mode_changes:
            out, _, _ = self.ssh.run(
                "redis-cli -n 4 keys 'BREAKOUT_CFG|*' | head -1", timeout=30
            )
            if not out.strip():
                hwsku_json = (
                    "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
                    "/Accton-WEDGE100S-32X/hwsku.json"
                )
                # Use list-form subprocess to avoid shell interpreting '|' in key names.
                seed_cmd = (
                    f"python3 -c \""
                    f"import json, subprocess; "
                    f"d=json.load(open('{hwsku_json}')); "
                    f"[subprocess.run(['redis-cli','-n','4','HSET','BREAKOUT_CFG|'+p,'brkout_mode',v['default_brkout_mode']]) "
                    f" for p,v in d['interfaces'].items()]"
                    f"\""
                )
                _, err, rc = self.ssh.run(seed_cmd, timeout=30)
                if rc != 0:
                    print(f"  [breakout] WARN: failed to seed BREAKOUT_CFG: {err.strip()}")
                else:
                    print("  [breakout] seeded BREAKOUT_CFG from hwsku.json", flush=True)

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
            out, err, rc = self.ssh.run(change.cmd, timeout=90)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

    def verify(self) -> bool:
        # Give orchagent time to settle after DPB + subport config before verifying.
        time.sleep(10)
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [breakout] FAIL: {c}")
            return False
        return True
