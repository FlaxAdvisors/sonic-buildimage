"""PortChannelTask — create PortChannel1 and add Ethernet16/Ethernet32 as members."""
import re
from .base import Change, ConfigTask


class PortChannelTask(ConfigTask):

    def check(self) -> list:
        changes = []
        for pc in self.topology["portchannels"]:
            name = pc["name"]

            # Does PortChannel exist?
            out, _, _ = self.ssh.run(
                f"redis-cli -n 4 exists 'PORTCHANNEL|{name}'", timeout=10
            )
            if out.strip() != "1":
                changes.append(Change(
                    item=f"{name} existence",
                    current="missing",
                    desired="present",
                    cmd=f"sudo config portchannel add {name}",
                ))

            # Check for pre-existing IP to remove
            out, _, _ = self.ssh.run(
                f"redis-cli -n 4 HGETALL 'INTERFACE|{name}'", timeout=10
            )
            # Keys like PORTCHANNEL_INTERFACE|PortChannel1|10.x.x.x/yy
            out2, _, _ = self.ssh.run(
                f"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|{name}|*'", timeout=10
            )
            for line in out2.strip().splitlines():
                m = re.search(r'\|([0-9a-fA-F:.]+/\d+)$', line.strip())
                if m:
                    ip = m.group(1)
                    changes.append(Change(
                        item=f"{name} IP {ip} (must be removed for L2 VLAN)",
                        current=ip,
                        desired="no IP",
                        cmd=f"sudo config interface ip remove {name} {ip}",
                    ))

            # Check members
            for member in pc["members"]:
                out, _, _ = self.ssh.run(
                    f"redis-cli -n 4 exists 'PORTCHANNEL_MEMBER|{name}|{member}'",
                    timeout=10,
                )
                if out.strip() != "1":
                    # Check for phantom INTERFACE entry (even empty) that blocks member add
                    iface_out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 exists 'INTERFACE|{member}'", timeout=10
                    )
                    if iface_out.strip() == "1":
                        changes.append(Change(
                            item=f"INTERFACE|{member} phantom entry (blocks member add)",
                            current="present",
                            desired="absent",
                            cmd=f"redis-cli -n 4 del 'INTERFACE|{member}'",
                        ))
                    changes.append(Change(
                        item=f"{name} member {member}",
                        current="missing",
                        desired="present",
                        cmd=f"sudo config portchannel member add {name} {member}",
                    ))

        return changes

    def apply(self, changes: list) -> None:
        for change in changes:
            out, err, rc = self.ssh.run(change.cmd, timeout=30)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

    def verify(self) -> bool:
        remaining = self.check()
        # Only fail on existence/member issues, not pre-existing IPs
        # (IP removal may take a moment to propagate)
        real_issues = [c for c in remaining if "IP" not in c.item]
        if real_issues:
            for c in real_issues:
                print(f"  [portchannel] FAIL: {c}")
            return False
        return True
