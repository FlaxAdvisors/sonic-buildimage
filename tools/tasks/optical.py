"""OpticalTask — set FEC and assert admin-up for optical ports."""
from .base import Change, ConfigTask


class OpticalTask(ConfigTask):

    def check(self) -> list:
        changes = []
        for op in self.topology["optical_ports"]:
            port = op["port"]
            desired_fec = op["fec"]

            # FEC mode
            out, _, _ = self.ssh.run(
                f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=10
            )
            current_fec = out.strip() or "unset"
            if current_fec != desired_fec:
                changes.append(Change(
                    item=f"{port} fec",
                    current=current_fec,
                    desired=desired_fec,
                    cmd=f"sudo config interface fec {port} {desired_fec}",
                ))

            # Admin status
            out, _, _ = self.ssh.run(
                f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
            )
            current_admin = out.strip() or "unset"
            if current_admin != "up":
                changes.append(Change(
                    item=f"{port} admin_status",
                    current=current_admin,
                    desired="up",
                    cmd=f"sudo config interface startup {port}",
                ))

        return changes

    def apply(self, changes: list) -> None:
        for change in changes:
            out, err, rc = self.ssh.run(change.cmd, timeout=15)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

    def verify(self) -> bool:
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [optical] FAIL: {c}")
            return False
        return True
