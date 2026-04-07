"""VlanTask — create VLANs and add members."""
from .base import Change, ConfigTask


class VlanTask(ConfigTask):

    def check(self) -> list:
        changes = []
        for vlan in self.topology["vlans"]:
            vid = vlan["id"]

            # VLAN exists?
            out, _, _ = self.ssh.run(
                f"redis-cli -n 4 exists 'VLAN|Vlan{vid}'", timeout=10
            )
            if out.strip() != "1":
                changes.append(Change(
                    item=f"VLAN {vid}",
                    current="missing",
                    desired="present",
                    cmd=f"sudo config vlan add {vid}",
                ))

            # Members
            for member in vlan["members"]:
                out, _, _ = self.ssh.run(
                    f"redis-cli -n 4 exists 'VLAN_MEMBER|Vlan{vid}|{member}'",
                    timeout=10,
                )
                if out.strip() != "1":
                    changes.append(Change(
                        item=f"VLAN {vid} member {member}",
                        current="missing",
                        desired="untagged member",
                        cmd=f"sudo config vlan member add --untagged {vid} {member}",
                    ))
                else:
                    # Verify tagging_mode is untagged
                    out, _, _ = self.ssh.run(
                        f"redis-cli -n 4 hget 'VLAN_MEMBER|Vlan{vid}|{member}' tagging_mode",
                        timeout=10,
                    )
                    if out.strip() != "untagged":
                        changes.append(Change(
                            item=f"VLAN {vid} member {member} tagging",
                            current=out.strip() or "unset",
                            desired="untagged",
                            cmd=f"sudo config vlan member del {vid} {member} && sudo config vlan member add --untagged {vid} {member}",
                        ))

        return changes

    def apply(self, changes: list) -> None:
        for change in changes:
            out, err, rc = self.ssh.run(change.cmd, timeout=30)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

    def verify(self) -> bool:
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [vlans] FAIL: {c}")
            return False
        return True
