# Deploy Tool and Test Framework Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tools/deploy.py` (idempotent L2-platform deployer) and refactor the test framework from save/reload/restore to assertive session checks against operational state.

**Architecture:** Two independent deliverables: (1) a new `tools/` tree with a `ConfigTask`-based deploy pipeline driven by `tools/topology.json`, and (2) test-framework surgery that removes `lib/prepost.py`, replaces `run_tests.py` with per-stage subprocess+JUnit abort, and rewrites bookend stages and several functional stages to read rather than write config.

**Tech Stack:** Python 3, paramiko (via `tests/lib/ssh_client.py`), pytest, JUnit XML stdlib (`xml.etree.ElementTree`), redis-cli, SONiC CLI (`config`, `show`, `sonic-cfggen`, `teamdctl`).

**Spec:** `docs/superpowers/specs/2026-03-21-deploy-and-test-framework-design.md`

---

## File Map

### New files
| File | Responsibility |
|---|---|
| `tools/topology.json` | Single source of truth: breakout ports, VLANs, PortChannel, optical ports, hosts |
| `tools/tasks/__init__.py` | Package marker |
| `tools/tasks/base.py` | `Change` dataclass + `ConfigTask` abstract base |
| `tools/tasks/mgmt_vrf.py` | `MgmtVrfTask` — VRF, eth0, SSH drop-in |
| `tools/tasks/breakout.py` | `BreakoutTask` — 4×25G breakout on three QSFP parents |
| `tools/tasks/portchannel.py` | `PortChannelTask` — PortChannel1 + IP removal |
| `tools/tasks/vlans.py` | `VlanTask` — VLAN 10 (hosts) + VLAN 999 (LAG) |
| `tools/tasks/optical.py` | `OpticalTask` — FEC rs + admin-up for four optical ports |
| `tools/deploy.py` | CLI entry point; drives task pipeline |
| `tests/stage_14_breakout/conftest.py` | Module fixture: break out Ethernet4, restore on teardown |
| `tests/stage_15_autoneg_fec/conftest.py` | Module fixture: capture + restore FEC on TEST_PORT |
| `tests/stage_21_lpmode/conftest.py` | Module fixture: capture + restore LP_MODE state |

### Modified files
| File | Change |
|---|---|
| `tests/conftest.py` | Add 5 assertive pre-checks + session-end health check |
| `tests/run_tests.py` | Per-stage subprocess invocation + JUnit XML abort logic |
| `tests/test_run_tests_unit.py` | Unit tests for new abort logic |
| `tests/stage_00_pretest/test_pretest.py` | Full rewrite → read-only operational audit |
| `tests/stage_nn_posttest/test_posttest.py` | Full rewrite → health check only |
| `tests/stage_13_link/test_link.py` | Remove configure_rsfec fixture; add optical port assertions |
| `tests/stage_14_breakout/test_breakout.py` | Change BREAKOUT_PORT + SPEED_TEST_PORT to Ethernet4 |
| `tests/stage_16_portchannel/test_portchannel.py` | Full rewrite → assert operational PortChannel1, L2-only failover |
| `tests/target.cfg.example` | Add `[hosts]` section |
| `tests/STAGED_PHASES.md` | Add Phase 22 as PENDING; update Phase 00, 16 status |

### Deleted files
| File | Reason |
|---|---|
| `tests/lib/prepost.py` | Replaced by assertive conftest + deploy tool |
| `tests/fixtures/clean_boot.json` | No longer used |

---

## DELIVERABLE 1: tools/deploy.py

---

### Task 1: topology.json

**Files:**
- Create: `tools/topology.json`

- [ ] **Step 1: Create the file**

```json
{
  "device": {
    "mgmt_gateway": "192.168.88.2",
    "mgmt_vrf": "mgmt"
  },
  "breakout_ports": [
    {"parent": "Ethernet0",  "mode": "4x25G[10G]"},
    {"parent": "Ethernet64", "mode": "4x25G[10G]"},
    {"parent": "Ethernet80", "mode": "4x25G[10G]"}
  ],
  "vlans": [
    {
      "id": 10,
      "members": [
        "Ethernet0","Ethernet1","Ethernet2","Ethernet3",
        "Ethernet64","Ethernet65","Ethernet66","Ethernet67",
        "Ethernet80","Ethernet81","Ethernet82","Ethernet83"
      ]
    },
    {
      "id": 999,
      "members": ["PortChannel1"]
    }
  ],
  "portchannels": [
    {"name": "PortChannel1", "members": ["Ethernet16","Ethernet32"]}
  ],
  "optical_ports": [
    {"port": "Ethernet100", "fec": "rs"},
    {"port": "Ethernet104", "fec": "rs"},
    {"port": "Ethernet108", "fec": "rs"},
    {"port": "Ethernet116", "fec": "rs"}
  ],
  "hosts": [
    {"port": "Ethernet0",  "mgmt_ip": "192.168.88.243", "test_ip": "10.0.10.243"},
    {"port": "Ethernet1",  "mgmt_ip": "192.168.88.232", "test_ip": "10.0.10.232"},
    {"port": "Ethernet66", "mgmt_ip": "192.168.88.237", "test_ip": "10.0.10.237"},
    {"port": "Ethernet67", "mgmt_ip": "192.168.88.242", "test_ip": "10.0.10.242"},
    {"port": "Ethernet80", "mgmt_ip": "192.168.88.225", "test_ip": "10.0.10.225"},
    {"port": "Ethernet81", "mgmt_ip": "192.168.88.241", "test_ip": "10.0.10.241"}
  ]
}
```

- [ ] **Step 2: Write unit test for topology validation helper (used in deploy.py)**

Create `tools/test_topology.py` (unit tests, no hardware needed):

```python
"""Unit tests for topology.json schema validation."""
import json, os, pytest

TOPOLOGY_PATH = os.path.join(os.path.dirname(__file__), "topology.json")

@pytest.fixture(scope="module")
def topology():
    with open(TOPOLOGY_PATH) as f:
        return json.load(f)

def test_required_keys(topology):
    for key in ("device", "breakout_ports", "vlans", "portchannels", "optical_ports", "hosts"):
        assert key in topology, f"Missing key: {key}"

def test_host_ports_in_vlan10(topology):
    vlan10 = next(v for v in topology["vlans"] if v["id"] == 10)
    vlan10_members = set(vlan10["members"])
    for h in topology["hosts"]:
        assert h["port"] in vlan10_members, (
            f"Host port {h['port']} not in VLAN 10 members: {vlan10_members}"
        )

def test_breakout_modes_valid(topology):
    valid_modes = {"1x100G[40G]", "4x25G[10G]", "4x10G"}
    for bp in topology["breakout_ports"]:
        assert bp["mode"] in valid_modes, f"Unknown mode: {bp['mode']}"

def test_portchannel_members_are_ports(topology):
    # PortChannel members should be un-broken-out ports
    breakout_parents = {bp["parent"] for bp in topology["breakout_ports"]}
    for pc in topology["portchannels"]:
        for member in pc["members"]:
            assert member not in breakout_parents, (
                f"PortChannel member {member} is a breakout parent"
            )

def test_optical_fec_values(topology):
    valid_fec = {"rs", "none", "fc"}
    for op in topology["optical_ports"]:
        assert op["fec"] in valid_fec, f"Unknown FEC: {op['fec']}"
```

- [ ] **Step 3: Run unit tests to verify they pass**

```bash
cd /export/sonic/sonic-buildimage.claude/tools
python3 -m pytest test_topology.py -v
```
Expected: 5 PASSED

- [ ] **Step 4: Commit**

```bash
git add tools/topology.json tools/test_topology.py
git commit -m "feat: add tools/topology.json with lab hardware topology"
```

---

### Task 2: tasks/base.py — Change + ConfigTask

**Files:**
- Create: `tools/tasks/__init__.py`
- Create: `tools/tasks/base.py`

- [ ] **Step 1: Write unit test for Change dataclass**

Create `tools/test_base.py`:

```python
"""Unit tests for ConfigTask base classes."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.tasks.base import Change, ConfigTask

def test_change_dataclass():
    c = Change(
        item="VRF mgmt",
        current="missing",
        desired="present",
        cmd="config vrf add mgmt",
    )
    assert c.item == "VRF mgmt"
    assert c.current == "missing"
    assert c.desired == "present"
    assert c.cmd == "config vrf add mgmt"

def test_change_repr_contains_item():
    c = Change(item="foo", current="a", desired="b", cmd="cmd")
    assert "foo" in repr(c)

class ConcreteTask(ConfigTask):
    def check(self):
        return []
    def apply(self, changes):
        pass
    def verify(self):
        return True

def test_concrete_task_instantiation():
    task = ConcreteTask(ssh=None, topology={})
    assert task.check() == []
    assert task.verify() is True
```

- [ ] **Step 2: Run to verify failure (ImportError)**

```bash
cd /export/sonic/sonic-buildimage.claude
python3 -m pytest tools/test_base.py -v
```
Expected: ImportError on `from tools.tasks.base import Change`

- [ ] **Step 3: Create package marker and base module**

`tools/tasks/__init__.py` — empty file.

`tools/tasks/base.py`:
```python
"""Base classes for ConfigTask pipeline."""
from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class Change:
    """Describes a single pending config change."""
    item: str     # human-readable description
    current: str  # observed current value
    desired: str  # target value
    cmd: str      # SONiC CLI command to apply this change

    def __repr__(self):
        return (
            f"Change({self.item!r}: {self.current!r} → {self.desired!r})"
        )


class ConfigTask(ABC):
    """Abstract base for all deploy tasks.

    Subclasses implement check(), apply(), and verify().
    deploy.py drives: check() → print changes → apply() → verify().
    """

    def __init__(self, ssh, topology: dict):
        self.ssh = ssh
        self.topology = topology

    @abstractmethod
    def check(self) -> list:
        """Query device state; return list[Change] for items that need updating.

        Must never modify state.
        """

    @abstractmethod
    def apply(self, changes: list) -> None:
        """Apply the list of changes returned by check()."""

    @abstractmethod
    def verify(self) -> bool:
        """Assert that post-apply state is correct. Return True on success."""
```

- [ ] **Step 4: Run unit tests to verify pass**

```bash
python3 -m pytest tools/test_base.py -v
```
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add tools/tasks/__init__.py tools/tasks/base.py tools/test_base.py
git commit -m "feat: add ConfigTask base class and Change dataclass"
```

---

### Task 3: tasks/mgmt_vrf.py — MgmtVrfTask

**Files:**
- Create: `tools/tasks/mgmt_vrf.py`

Key behaviors (from spec §3.3):
1. Assert `ip vrf show` contains `mgmt`; create if missing
2. Assert eth0 is `master mgmt`; move if missing
3. Assert default route in mgmt table; add if missing
4. Assert `MGMT_VRF_CONFIG|vrf_global.mgmtVrfEnabled == true` in CONFIG_DB; set if missing
5. Assert SSH drop-in at `/etc/systemd/system/ssh.service.d/sonic.conf` has correct `ExecStart` line; write + daemon-reload + restart ssh if missing/wrong
6. After SSH restart: reconnect with 2 s retry, 30 s max; exit on failure

- [ ] **Step 1: Create mgmt_vrf.py**

```python
"""MgmtVrfTask — ensure management VRF is configured and SSH is in VRF."""
import time
from .base import Change, ConfigTask

SSH_DROP_IN_PATH = "/etc/systemd/system/ssh.service.d/sonic.conf"
SSH_EXEC_START = "ExecStart=/usr/bin/ip vrf exec mgmt /usr/sbin/sshd -D $SSHD_OPTS"


class MgmtVrfTask(ConfigTask):

    def check(self) -> list:
        changes = []
        gw = self.topology["device"]["mgmt_gateway"]
        vrf = self.topology["device"]["mgmt_vrf"]

        # 1. VRF existence
        out, _, _ = self.ssh.run("ip vrf show", timeout=10)
        if vrf not in out:
            changes.append(Change(
                item=f"VRF {vrf}",
                current="missing",
                desired="present",
                cmd=f"sudo config vrf add {vrf}",
            ))

        # 2. eth0 in VRF
        out, _, _ = self.ssh.run("ip link show eth0", timeout=10)
        if f"master {vrf}" not in out:
            changes.append(Change(
                item=f"eth0 master {vrf}",
                current="not in VRF",
                desired=f"master {vrf}",
                cmd=f"sudo ip link set eth0 master {vrf}",
            ))

        # 3. Default route in mgmt routing table
        out, _, _ = self.ssh.run("ip route show table 1000", timeout=10)
        if "default" not in out:
            changes.append(Change(
                item="mgmt default route",
                current="missing",
                desired=f"via {gw}",
                cmd=f"sudo ip route add default via {gw} table 1000",
            ))

        # 4. MGMT_VRF_CONFIG in CONFIG_DB
        out, _, _ = self.ssh.run(
            "redis-cli -n 4 hget 'MGMT_VRF_CONFIG|vrf_global' mgmtVrfEnabled",
            timeout=10,
        )
        if out.strip() != "true":
            changes.append(Change(
                item="MGMT_VRF_CONFIG|vrf_global.mgmtVrfEnabled",
                current=out.strip() or "unset",
                desired="true",
                cmd="sudo config vrf add mgmt",  # sets CONFIG_DB entry
            ))

        # 5. SSH drop-in
        out, _, rc = self.ssh.run(
            f"grep -F '{SSH_EXEC_START}' {SSH_DROP_IN_PATH} 2>/dev/null",
            timeout=10,
        )
        if rc != 0:
            changes.append(Change(
                item="SSH VRF drop-in",
                current="missing or incorrect",
                desired="ExecStart in VRF",
                cmd=f"_write_ssh_dropin",  # sentinel — handled specially in apply()
            ))

        return changes

    def apply(self, changes: list) -> None:
        needs_ssh_restart = False
        for change in changes:
            if change.cmd == "_write_ssh_dropin":
                needs_ssh_restart = True
                self._write_ssh_dropin()
            else:
                out, err, rc = self.ssh.run(change.cmd, timeout=30)
                if rc != 0:
                    print(f"  [warn] {change.cmd!r} returned rc={rc}: {err.strip()}")

        if needs_ssh_restart:
            print("  [mgmt_vrf] Restarting SSH (applying drop-in)...", flush=True)
            self.ssh.run(
                "sudo systemctl daemon-reload && sudo systemctl restart ssh",
                timeout=15,
            )
            self._reconnect_ssh()

    def verify(self) -> bool:
        remaining = self.check()
        if remaining:
            for c in remaining:
                print(f"  [mgmt_vrf] FAIL: {c}")
            return False
        return True

    # ------------------------------------------------------------------ helpers

    def _write_ssh_dropin(self):
        dropin_content = "[Service]\n" + SSH_EXEC_START + "\n"
        self.ssh.run(
            "sudo mkdir -p /etc/systemd/system/ssh.service.d",
            timeout=10,
        )
        self.ssh.run(
            f"printf '%s' '{dropin_content}' | sudo tee {SSH_DROP_IN_PATH} > /dev/null",
            timeout=10,
        )

    def _reconnect_ssh(self):
        """Reconnect SSH with 2s retry, 30s total. Exit on failure."""
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.ssh.connect()
                return
            except Exception:
                time.sleep(2)
        raise SystemExit(
            "ERROR: SSH did not recover after VRF restart — "
            "check drop-in manually and re-run deploy.py"
        )
```

- [ ] **Step 2: Hardware integration smoke-test**

Run against the switch to verify check() returns an empty list when VRF is already configured:

```bash
ssh admin@192.168.88.12 "ip vrf show && ip link show eth0 && ip route show table 1000"
```
Expected: `mgmt` in VRF list, `eth0` shows `master mgmt`, default route exists.

- [ ] **Step 3: Commit**

```bash
git add tools/tasks/mgmt_vrf.py
git commit -m "feat: add MgmtVrfTask for management VRF and SSH drop-in config"
```

---

### Task 4: tasks/breakout.py — BreakoutTask

**Files:**
- Create: `tools/tasks/breakout.py`

Key behaviors:
- `check()`: reads `BREAKOUT_CFG|<parent>.brkout_mode` in CONFIG_DB for each parent in `topology.json`
- `apply()`: issues all needed `config interface breakout <parent> <mode>` commands in one pass, no waiting
- `verify()`: polls `redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP` until all 12 expected sub-ports appear; 3s poll, 120s timeout

The 12 expected sub-ports come from expanding breakout parents: Ethernet0→0/1/2/3, Ethernet64→64/65/66/67, Ethernet80→80/81/82/83.

Note: Ethernet64's `4x25G[10G]` mode produces sub-ports Ethernet64, Ethernet65, Ethernet66, Ethernet67 (not 4x25G but 4x10G hardware; the breakout mode string is `4x25G[10G]` which is what SONiC calls it on Tomahawk).

- [ ] **Step 1: Create breakout.py**

```python
"""BreakoutTask — break out QSFP parent ports to sub-ports."""
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
        return changes

    def apply(self, changes: list) -> None:
        for change in changes:
            out, err, rc = self.ssh.run(change.cmd, timeout=60)
            if rc != 0:
                print(f"  [warn] {change.cmd!r} rc={rc}: {err.strip()}")

    def verify(self) -> bool:
        expected = _expected_subports(self.topology["breakout_ports"])
        deadline = time.time() + 120
        poll_interval = 3
        while time.time() < deadline:
            out, _, _ = self.ssh.run(
                "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP",
                timeout=15,
            )
            present = set(out.split())
            missing = [p for p in expected if p not in present]
            if not missing:
                return True
            print(
                f"  [breakout] waiting for sub-ports: {missing[:4]}{'...' if len(missing) > 4 else ''}",
                flush=True,
            )
            time.sleep(poll_interval)
        print(f"  [breakout] TIMEOUT: sub-ports still missing: {missing}")
        return False
```

- [ ] **Step 2: Write unit test for _expected_subports helper**

Add to `tools/test_base.py`:

```python
from tools.tasks.breakout import _expected_subports

def test_expected_subports_expansion():
    bps = [
        {"parent": "Ethernet0",  "mode": "4x25G[10G]"},
        {"parent": "Ethernet64", "mode": "4x25G[10G]"},
    ]
    result = _expected_subports(bps)
    assert result == [
        "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
        "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    ]
```

- [ ] **Step 3: Run unit tests**

```bash
python3 -m pytest tools/test_base.py -v
```
Expected: 4 PASSED

- [ ] **Step 4: Commit**

```bash
git add tools/tasks/breakout.py tools/test_base.py
git commit -m "feat: add BreakoutTask with COUNTERS_PORT_NAME_MAP polling"
```

---

### Task 5: tasks/portchannel.py — PortChannelTask

**Files:**
- Create: `tools/tasks/portchannel.py`

Key behaviors:
- Create PortChannel1 with `lacp_mode: active` if missing
- Detect existing IP on PortChannel1 via `redis-cli -n 4 HGETALL 'INTERFACE|PortChannel1'`; remove it if present
- Add Ethernet16 and Ethernet32 as members

- [ ] **Step 1: Create portchannel.py**

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add tools/tasks/portchannel.py
git commit -m "feat: add PortChannelTask with IP detection/removal"
```

---

### Task 6: tasks/vlans.py — VlanTask

**Files:**
- Create: `tools/tasks/vlans.py`

Key behaviors:
- Create VLANs 10 and 999 if missing
- Add all VLAN 10 sub-ports as access members
- Add PortChannel1 to VLAN 999 as access member

- [ ] **Step 1: Create vlans.py**

```python
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
                        desired="access member",
                        cmd=(
                            f"sudo config vlan member add {vid} {member}"
                            if member.startswith("Ethernet")
                            else f"sudo config vlan member add {vid} {member}"
                        ),
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
```

- [ ] **Step 2: Commit**

```bash
git add tools/tasks/vlans.py
git commit -m "feat: add VlanTask for VLAN 10 and VLAN 999 membership"
```

---

### Task 7: tasks/optical.py — OpticalTask

**Files:**
- Create: `tools/tasks/optical.py`

Key behaviors:
- For each port in `optical_ports`: assert FEC mode in CONFIG_DB; assert admin_status=up
- `apply()` issues `config interface fec <port> <mode>` and `config interface startup <port>`

- [ ] **Step 1: Create optical.py**

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add tools/tasks/optical.py
git commit -m "feat: add OpticalTask for FEC and admin-up on optical ports"
```

---

### Task 8: deploy.py — CLI entry point

**Files:**
- Create: `tools/deploy.py`

Key behaviors:
- Execution order: MgmtVrfTask → BreakoutTask → PortChannelTask → VlanTask → OpticalTask → `config save -y`
- `--dry-run`: call check() on all tasks, print Change list, exit without applying
- `--task <name>`: run only the named task
- Startup validation: every `hosts[].port` must be in VLAN 10 members; exit on divergence

- [ ] **Step 1: Write unit test for topology validation function**

Add to `tools/test_topology.py`:

```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
# We'll import the validation helper once deploy.py exists
# For now, test topology.json data integrity only (Task 1 tests cover this)
```

- [ ] **Step 2: Create deploy.py**

```python
#!/usr/bin/env python3
"""deploy.py — idempotent SONiC L2 platform deployer.

Usage:
    tools/deploy.py [--dry-run] [--target-cfg tests/target.cfg]
                    [--topology tools/topology.json]
                    [--task mgmt_vrf|breakout|portchannel|vlans|optical]
"""

import argparse
import json
import os
import sys

# Allow running as tools/deploy.py from repo root
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
sys.path.insert(0, _REPO_ROOT)

from tests.lib.ssh_client import SSHClient
from tools.tasks.mgmt_vrf import MgmtVrfTask
from tools.tasks.breakout import BreakoutTask
from tools.tasks.portchannel import PortChannelTask
from tools.tasks.vlans import VlanTask
from tools.tasks.optical import OpticalTask

TASK_ORDER = [
    ("mgmt_vrf",    MgmtVrfTask),
    ("breakout",    BreakoutTask),
    ("portchannel", PortChannelTask),
    ("vlans",       VlanTask),
    ("optical",     OpticalTask),
]

DEFAULT_TARGET_CFG = os.path.join(_REPO_ROOT, "tests", "target.cfg")
DEFAULT_TOPOLOGY   = os.path.join(_TOOLS_DIR, "topology.json")


def _validate_topology(topology: dict) -> None:
    """Verify topology self-consistency. Exit on divergence."""
    vlan10 = next((v for v in topology["vlans"] if v["id"] == 10), None)
    if vlan10 is None:
        sys.exit("ERROR: topology.json has no VLAN 10 entry")
    vlan10_members = set(vlan10["members"])
    for host in topology["hosts"]:
        if host["port"] not in vlan10_members:
            sys.exit(
                f"ERROR: hosts[].port={host['port']!r} is not in VLAN 10 members.\n"
                f"  VLAN 10 members: {sorted(vlan10_members)}\n"
                "Fix topology.json before running deploy."
            )


def _run_task(name: str, task_cls, ssh, topology: dict, dry_run: bool) -> bool:
    print(f"\n{'='*60}", flush=True)
    print(f"  Task: {name}", flush=True)
    print(f"{'='*60}", flush=True)

    task = task_cls(ssh=ssh, topology=topology)
    changes = task.check()

    if not changes:
        print(f"  [OK] no changes needed")
        return True

    for c in changes:
        print(f"  CHANGE: {c}")

    if dry_run:
        print(f"  [dry-run] skipping apply")
        return True

    task.apply(changes)
    ok = task.verify()
    if ok:
        print(f"  [OK] verified")
    else:
        print(f"  [FAIL] verify failed after apply")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Deploy SONiC L2 platform config")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes but do not apply")
    parser.add_argument("--target-cfg", default=DEFAULT_TARGET_CFG,
                        help=f"Path to target.cfg (default: {DEFAULT_TARGET_CFG})")
    parser.add_argument("--topology", default=DEFAULT_TOPOLOGY,
                        help=f"Path to topology.json (default: {DEFAULT_TOPOLOGY})")
    parser.add_argument("--task",
                        choices=[name for name, _ in TASK_ORDER],
                        help="Run only this task")
    args = parser.parse_args()

    # Load and validate topology
    if not os.path.exists(args.topology):
        sys.exit(f"ERROR: topology not found: {args.topology}")
    with open(args.topology) as f:
        topology = json.load(f)
    _validate_topology(topology)

    # Connect
    if not os.path.exists(args.target_cfg):
        sys.exit(
            f"ERROR: target config not found: {args.target_cfg}\n"
            "Copy tests/target.cfg.example to tests/target.cfg and fill in credentials."
        )
    print(f"Connecting to target ({args.target_cfg})...", flush=True)
    ssh = SSHClient(args.target_cfg)
    try:
        ssh.connect()
    except Exception as e:
        sys.exit(f"ERROR: SSH connection failed: {e}")

    out, _, rc = ssh.run("uname -n", timeout=10)
    print(f"Connected to: {out.strip()}")

    # Run tasks
    tasks_to_run = (
        [(args.task, cls) for name, cls in TASK_ORDER if name == args.task]
        if args.task
        else TASK_ORDER
    )

    all_ok = True
    for name, task_cls in tasks_to_run:
        ok = _run_task(name, task_cls, ssh, topology, dry_run=args.dry_run)
        if not ok:
            all_ok = False
            print(f"\nERROR: task {name!r} failed. Stopping.", flush=True)
            break

    if all_ok and not args.dry_run and not args.task:
        print("\n  Saving config...", flush=True)
        out, err, rc = ssh.run("sudo config save -y", timeout=60)
        if rc != 0:
            print(f"  [warn] config save failed: {err.strip()}")
        else:
            print("  [OK] config saved")

    ssh.close()

    if not all_ok:
        sys.exit(1)
    print("\nDeploy complete." if not args.dry_run else "\nDry-run complete.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write unit test for topology validation**

Add to `tools/test_topology.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.deploy import _validate_topology

def test_validate_topology_passes_with_valid_data(topology):
    """Valid topology.json passes validation without raising SystemExit."""
    _validate_topology(topology)  # should not raise

def test_validate_topology_fails_on_missing_host_port():
    bad = {
        "vlans": [{"id": 10, "members": ["Ethernet0"]}],
        "hosts": [{"port": "Ethernet99", "mgmt_ip": "1.2.3.4", "test_ip": "2.3.4.5"}],
    }
    with pytest.raises(SystemExit, match="Ethernet99"):
        _validate_topology(bad)
```

- [ ] **Step 4: Run unit tests**

```bash
python3 -m pytest tools/test_topology.py tools/test_base.py -v
```
Expected: All PASSED

- [ ] **Step 5: Smoke test deploy --dry-run against switch**

```bash
cd /export/sonic/sonic-buildimage.claude
python3 tools/deploy.py --dry-run
```
Expected: connects, prints Change list (or "no changes needed" per task), exits 0.

- [ ] **Step 6: Commit**

```bash
git add tools/deploy.py tools/test_topology.py
git commit -m "feat: add deploy.py CLI with task pipeline and topology validation"
```

---

## DELIVERABLE 2: Test Framework Refactor

---

### Task 9: run_tests.py — per-stage subprocess + JUnit abort

**Files:**
- Modify: `tests/run_tests.py`
- Modify: `tests/test_run_tests_unit.py`

The current `_run_tests()` calls pytest once for all stages. New behavior: iterate stages, call pytest once per stage via subprocess with `--junitxml=<tmpfile>`, parse XML, apply abort logic.

**Abort rules (per spec §4.2):**
- `tests == 0`: skip, continue
- `tests > 0` and `passed == 0` and `skipped < tests`: complete failure → abort
- `tests > 0` and `passed == 0` and `skipped == tests`: all skipped → continue
- Otherwise: continue
- Note: xfail counts as passed

- [ ] **Step 1: Write failing unit tests for abort logic**

Add to `tests/test_run_tests_unit.py`:

```python
import run_tests

def test_abort_on_all_failed():
    """All tests failed (none passed/skipped) → should abort."""
    assert run_tests._should_abort(tests=3, passed=0, skipped=0, xfailed=0) is True

def test_no_abort_when_some_passed():
    """Some passed → continue."""
    assert run_tests._should_abort(tests=3, passed=1, skipped=1, xfailed=0) is False

def test_no_abort_when_all_skipped():
    """All skipped → continue (host stage when hosts unreachable)."""
    assert run_tests._should_abort(tests=3, passed=0, skipped=3, xfailed=0) is False

def test_no_abort_when_zero_tests():
    """No tests collected → treat as skip."""
    assert run_tests._should_abort(tests=0, passed=0, skipped=0, xfailed=0) is False

def test_no_abort_when_xfail_fills_passed():
    """xfail counts as passed — stage with only xfail is not a failure."""
    assert run_tests._should_abort(tests=2, passed=0, skipped=0, xfailed=2) is False

def test_partial_failure_continues():
    """Some failed, some passed → continue."""
    assert run_tests._should_abort(tests=5, passed=2, skipped=0, xfailed=0) is False
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest test_run_tests_unit.py::test_abort_on_all_failed -v
```
Expected: AttributeError — `_should_abort` does not exist yet.

- [ ] **Step 3: Rewrite _run_tests() and add _should_abort() in run_tests.py**

Replace the existing `_run_tests()` function and add helpers:

```python
import tempfile
import xml.etree.ElementTree as ET


def _should_abort(tests: int, passed: int, skipped: int, xfailed: int) -> bool:
    """Return True if the stage result warrants aborting remaining stages.

    xfail counts as passed. A stage completely fails only when tests > 0,
    effective_passed == 0, and skipped < tests (i.e. some tests actually ran
    and failed or errored).
    """
    if tests == 0:
        return False
    effective_passed = passed + xfailed
    if effective_passed == 0 and skipped < tests:
        return True
    return False


def _parse_junit(xml_path: str) -> dict:
    """Parse a JUnit XML file; return counts dict with keys:
    tests, passed, failed, errored, skipped, xfailed.
    """
    try:
        tree = ET.parse(xml_path)
    except Exception:
        return dict(tests=0, passed=0, failed=0, errored=0, skipped=0, xfailed=0)

    root = tree.getroot()
    # pytest --junitxml produces a <testsuite> as root or nested under <testsuites>
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return dict(tests=0, passed=0, failed=0, errored=0, skipped=0, xfailed=0)

    tests    = int(suite.get("tests",    0))
    failures = int(suite.get("failures", 0))
    errors   = int(suite.get("errors",   0))
    skipped  = int(suite.get("skipped",  0))

    # Count xfail from individual <testcase> elements
    xfailed = sum(
        1 for tc in suite.findall("testcase")
        if tc.find("skipped") is not None
        and "xfail" in (tc.find("skipped").get("message", "") or "").lower()
    )
    # xfail appears as "skipped" in JUnit XML from pytest
    # Adjust: xfailed tests counted above were already counted in skipped
    skipped_real = skipped - xfailed

    passed = tests - failures - errors - skipped

    return dict(
        tests=tests,
        passed=passed,
        failed=failures,
        errored=errors,
        skipped=skipped_real,
        xfailed=xfailed,
    )


def _run_tests(stage_names, cfg_path, extra_pytest_args, inject_prepost=True):
    stage_names = _inject_prepost(stage_names, inject=inject_prepost)
    available = set(_available_stages())
    stage_names = [s for s in stage_names if s in available]

    print("=" * 64)
    print("  Wedge 100S-32X SONiC Platform Test Suite")
    print(f"  Stages: {', '.join(stage_names)}")
    print("=" * 64)

    overall_rc = 0

    for stage in stage_names:
        stage_dir = os.path.join(TESTS_DIR, stage)
        print(f"\n{'─'*64}")
        print(f"  Running: {stage}")
        print(f"{'─'*64}")

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
            junit_path = tf.name

        cmd = (
            [sys.executable, "-m", "pytest", "--target-cfg", cfg_path,
             f"--junitxml={junit_path}", stage_dir]
            + extra_pytest_args
        )
        result = subprocess.run(cmd, cwd=TESTS_DIR)
        stage_rc = result.returncode
        if stage_rc != 0:
            overall_rc = stage_rc

        counts = _parse_junit(junit_path)
        try:
            os.unlink(junit_path)
        except OSError:
            pass

        print(
            f"  Result: tests={counts['tests']} passed={counts['passed']} "
            f"failed={counts['failed']} skipped={counts['skipped']} "
            f"xfailed={counts['xfailed']}",
            flush=True,
        )

        if _should_abort(
            tests=counts["tests"],
            passed=counts["passed"],
            skipped=counts["skipped"],
            xfailed=counts["xfailed"],
        ):
            print(f"\n  ABORT: {stage} completely failed — stopping test run.")
            sys.exit(overall_rc or 1)

    sys.exit(overall_rc)
```

Also remove the `import tempfile` and `import xml.etree.ElementTree` additions — they go at the top of the file with the other imports.

- [ ] **Step 4: Run unit tests**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest test_run_tests_unit.py -v
```
Expected: All 9 tests PASSED (3 original + 6 new)

- [ ] **Step 5: Commit**

```bash
git add tests/run_tests.py tests/test_run_tests_unit.py
git commit -m "feat: per-stage JUnit abort logic in run_tests.py"
```

---

### Task 10: conftest.py — assertive session checks

**Files:**
- Modify: `tests/conftest.py`

Add 5 assertive checks to `pytest_sessionstart` after the existing SSH connect + shell sanity. Add session-end health check to `pytest_sessionfinish`.

- [ ] **Step 1: Add pre-checks and post-check to conftest.py**

After the `print(f"[target] Connected...")` line in `pytest_sessionstart`, add:

```python
    # ── Assertive pre-checks ──────────────────────────────────────────────
    # These fail fast before any test collects, directing the user to
    # run tools/deploy.py if the switch is not in operational state.

    # 1. pmon running
    out, _, rc = client.run("sudo systemctl is-active pmon 2>&1", timeout=15)
    if rc != 0 or "active" not in out:
        client.close()
        pytest.exit(
            "\n[target] pmon is not active.\n"
            "Run: sudo systemctl start pmon\n",
            returncode=2,
        )

    # 2. mgmt VRF present
    out, _, rc = client.run("ip vrf show", timeout=10)
    if "mgmt" not in out:
        client.close()
        pytest.exit(
            "\n[target] mgmt VRF missing — run: tools/deploy.py\n",
            returncode=3,
        )

    # 3. Breakout sub-ports in COUNTERS_PORT_NAME_MAP (ASIC_DB DB2)
    _EXPECTED_SUBPORTS = [
        "Ethernet0","Ethernet1","Ethernet2","Ethernet3",
        "Ethernet64","Ethernet65","Ethernet66","Ethernet67",
        "Ethernet80","Ethernet81","Ethernet82","Ethernet83",
    ]
    out, _, _ = client.run(
        "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
    )
    missing_subports = [p for p in _EXPECTED_SUBPORTS if p not in out.split()]
    if missing_subports:
        client.close()
        pytest.exit(
            f"\n[target] breakout sub-ports missing: {missing_subports}\n"
            "Run: tools/deploy.py --task breakout\n",
            returncode=3,
        )

    # 4. PortChannel1 present in CONFIG_DB
    out, _, _ = client.run(
        r"redis-cli -n 4 EXISTS 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    if out.strip() != "1":
        client.close()
        pytest.exit(
            "\n[target] PortChannel1 missing — run: tools/deploy.py\n",
            returncode=3,
        )

    # 5. VLAN 10 and VLAN 999 present in CONFIG_DB
    out10, _, _ = client.run(
        r"redis-cli -n 4 EXISTS 'VLAN|Vlan10'", timeout=10
    )
    out999, _, _ = client.run(
        r"redis-cli -n 4 EXISTS 'VLAN|Vlan999'", timeout=10
    )
    if out10.strip() != "1" or out999.strip() != "1":
        client.close()
        pytest.exit(
            "\n[target] VLANs missing (need Vlan10 and Vlan999) — run: tools/deploy.py\n",
            returncode=3,
        )

    print("[target] Pre-checks: pmon OK, mgmt VRF OK, breakout OK, PortChannel1 OK, VLANs OK\n",
          flush=True)
```

Replace `pytest_sessionfinish` with:

```python
def pytest_sessionfinish(session, exitstatus):
    """Close the SSH connection and run end-of-session health check."""
    global _SSH_CLIENT
    if _SSH_CLIENT is not None:
        client = _SSH_CLIENT
        # Health check
        out, _, rc = client.run("sudo systemctl is-active pmon 2>&1", timeout=10)
        if rc != 0:
            print("\n[target] WARNING: pmon is not active at session end.", flush=True)
        # Check for crashed containers
        out, _, _ = client.run(
            "docker ps --format '{{.Names}} {{.Status}}' | grep -E 'Exited|Error' || true",
            timeout=10,
        )
        if out.strip():
            print(f"\n[target] WARNING: crashed containers:\n{out.strip()}", flush=True)
        client.close()
        _SSH_CLIENT = None
```

- [ ] **Step 2: Verify conftest.py works with a quick test run**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_02_system/ -v --no-header 2>&1 | head -30
```
Expected: Pre-checks pass, tests run normally.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "feat: add assertive pre/post session checks in conftest.py"
```

---

### Task 11: stage_00_pretest — operational audit

**Files:**
- Modify: `tests/stage_00_pretest/test_pretest.py`

Remove all save/reload/restore logic. Rewrite as read-only audit that verifies deploy.py ran correctly. Failure means "run tools/deploy.py".

- [ ] **Step 1: Rewrite test_pretest.py**

```python
"""Stage 00 — Pre-Test: Operational state audit.

Verifies that tools/deploy.py has been run and the switch is in the
expected operational state before any functional tests execute.

Failure here means "run tools/deploy.py" — these are NOT test failures
in the traditional sense; they indicate missing prerequisite config.
"""

import pytest

CONNECTED_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
BREAKOUT_SUBPORTS = [
    "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
    "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    "Ethernet80", "Ethernet81", "Ethernet82", "Ethernet83",
]


def test_pmon_running(ssh):
    """pmon service is active."""
    out, _, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active: {out.strip()}\nFix: sudo systemctl start pmon"


def test_mgmt_vrf_present(ssh):
    """mgmt VRF is configured."""
    out, _, rc = ssh.run("ip vrf show", timeout=10)
    assert "mgmt" in out, "mgmt VRF missing — run: tools/deploy.py"


def test_breakout_subports_in_asic_db(ssh):
    """All 12 breakout sub-ports are present in COUNTERS_PORT_NAME_MAP (ASIC_DB)."""
    out, _, _ = ssh.run(
        "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
    )
    present = set(out.split())
    missing = [p for p in BREAKOUT_SUBPORTS if p not in present]
    assert not missing, (
        f"Breakout sub-ports missing in ASIC_DB: {missing}\n"
        "Fix: tools/deploy.py --task breakout"
    )


def test_portchannel1_in_config_db(ssh):
    """PortChannel1 exists in CONFIG_DB."""
    out, _, _ = ssh.run(
        r"redis-cli -n 4 EXISTS 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    assert out.strip() == "1", (
        "PortChannel1 missing in CONFIG_DB — run: tools/deploy.py"
    )


def test_portchannel1_has_no_ip(ssh):
    """PortChannel1 has no IP address (L2 VLAN 999 only)."""
    out, _, _ = ssh.run(
        r"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|PortChannel1|*'", timeout=10
    )
    assert not out.strip(), (
        f"PortChannel1 has IP configured (L2 only expected): {out.strip()}\n"
        "Fix: tools/deploy.py --task portchannel"
    )


def test_vlan10_and_999_exist(ssh):
    """VLAN 10 and VLAN 999 are present in CONFIG_DB."""
    for vid in (10, 999):
        out, _, _ = ssh.run(
            f"redis-cli -n 4 EXISTS 'VLAN|Vlan{vid}'", timeout=10
        )
        assert out.strip() == "1", (
            f"VLAN {vid} missing in CONFIG_DB — run: tools/deploy.py"
        )


def test_vlan10_has_breakout_members(ssh):
    """All 12 breakout sub-ports are VLAN 10 members."""
    missing = []
    for port in BREAKOUT_SUBPORTS:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 EXISTS 'VLAN_MEMBER|Vlan10|{port}'", timeout=10
        )
        if out.strip() != "1":
            missing.append(port)
    assert not missing, (
        f"Ports missing from VLAN 10: {missing}\nFix: tools/deploy.py --task vlans"
    )


def test_connected_ports_admin_up(ssh):
    """Connected uplink ports (Ethernet16/32/48/112) are admin-up."""
    for port in CONNECTED_PORTS:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
        )
        assert out.strip() == "up", (
            f"{port} admin_status={out.strip()!r} — expected 'up'\n"
            f"Fix: sudo config interface startup {port}"
        )


def test_optical_ports_fec_configured(ssh):
    """Optical ports (Ethernet100/104/108/116) have FEC=rs in CONFIG_DB."""
    optical = ["Ethernet100", "Ethernet104", "Ethernet108", "Ethernet116"]
    for port in optical:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=10
        )
        assert out.strip() == "rs", (
            f"{port} fec={out.strip()!r} — expected 'rs'\n"
            f"Fix: tools/deploy.py --task optical"
        )
```

- [ ] **Step 2: Run stage_00 against hardware to verify it passes**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_00_pretest/ -v
```
Expected: All tests PASS (deploy.py must have been run first).

- [ ] **Step 3: Commit**

```bash
git add tests/stage_00_pretest/test_pretest.py
git commit -m "refactor: stage_00 → read-only operational audit, remove save/reload"
```

---

### Task 12: stage_nn_posttest — health check

**Files:**
- Modify: `tests/stage_nn_posttest/test_posttest.py`

Remove all restore/snapshot logic. Keep only health assertions.

- [ ] **Step 1: Rewrite test_posttest.py**

```python
"""Stage NN — Post-Test: Health check.

Verifies the switch is in a healthy state after all test stages complete.
Does NOT restore config — tests run against operational state established
by tools/deploy.py and should not require cleanup.
"""

import pytest
import re


def test_pmon_running(ssh):
    """pmon service is still active after all test stages."""
    out, _, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active after tests: {out.strip()}"


def test_ssh_responsive(ssh):
    """SSH shell is responsive (basic sanity)."""
    out, _, rc = ssh.run("echo pong", timeout=10)
    assert rc == 0 and "pong" in out, "SSH shell not responding"


def test_no_crashed_containers(ssh):
    """No Docker containers are in Exited or Error state."""
    out, _, rc = ssh.run(
        "docker ps --format '{{.Names}} {{.Status}}'", timeout=15
    )
    assert rc == 0, f"docker ps failed: {out}"
    crashed = [
        line for line in out.splitlines()
        if re.search(r'\b(Exited|Error)\b', line, re.IGNORECASE)
    ]
    assert not crashed, (
        f"Crashed containers detected after test run:\n"
        + "\n".join(f"  {c}" for c in crashed)
    )


def test_portchannel1_still_active(ssh):
    """PortChannel1 still present in CONFIG_DB after tests."""
    out, _, _ = ssh.run(
        r"redis-cli -n 4 EXISTS 'PORTCHANNEL|PortChannel1'", timeout=10
    )
    assert out.strip() == "1", (
        "PortChannel1 disappeared from CONFIG_DB — a test may have deleted it"
    )
```

- [ ] **Step 2: Run against hardware**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_nn_posttest/ -v
```
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/stage_nn_posttest/test_posttest.py
git commit -m "refactor: stage_nn → health check only, remove config restore"
```

---

### Task 13: stage_13_link — remove FEC fixture, add optical assertions

**Files:**
- Modify: `tests/stage_13_link/test_link.py`

Changes:
1. Remove `configure_rsfec` session fixture (FEC is already set by deploy.py; removing it in teardown would break the switch)
2. Add `test_optical_ports_fec_in_config_db` — assert Ethernet100/104/108/116 have FEC=rs and admin=up
3. Add `test_ethernet108_lldp_visible` — assert Ethernet108 has an LLDP neighbor (SR4 fiber connected)
4. Keep all existing connectivity tests unchanged

- [ ] **Step 1: Remove the configure_rsfec fixture and add optical tests**

Delete these lines from `test_link.py` (lines 61-77):
```python
@pytest.fixture(scope="session", autouse=True)
def configure_rsfec(ssh):
    """Configure RS-FEC on connected ports; remove after stage completes."""
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    # Wait for links to come up (up to 30 s)
    import time
    deadline = time.time() + 30
    while time.time() < deadline:
        out, _, rc = ssh.run("show interfaces status 2>&1", timeout=15)
        up_ports = [l for l in out.splitlines() if any(p in l for p in CONNECTED_PORTS) and " up " in l]
        if len(up_ports) >= 2:  # at least 2 of 4 up (Ethernet104/108 blocked)
            break
        time.sleep(3)
    yield
    # Teardown: remove FEC
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)
```

Add after the existing `# ------ LLDP neighbor discovery ------` section:

```python
# ------------------------------------------------------------------
# Optical port configuration (Ethernet100/104/108/116)
# ------------------------------------------------------------------

OPTICAL_PORTS = ["Ethernet100", "Ethernet104", "Ethernet108", "Ethernet116"]


def test_optical_ports_fec_rs_in_config_db(ssh):
    """Optical ports (Ethernet100/104/108/116) have fec=rs in CONFIG_DB.

    Set by tools/deploy.py OpticalTask. All four ports use RS-FEC
    regardless of module type (SR4, LR4, CWDM4).
    """
    for port in OPTICAL_PORTS:
        out, _, rc = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' fec", timeout=10
        )
        fec = out.strip()
        print(f"  {port}: fec={fec!r}")
        assert fec == "rs", (
            f"{port}: fec={fec!r}, expected 'rs'.\n"
            "Fix: tools/deploy.py --task optical"
        )


def test_optical_ports_admin_up(ssh):
    """Optical ports are admin-up in CONFIG_DB."""
    for port in OPTICAL_PORTS:
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
        )
        admin = out.strip()
        print(f"  {port}: admin_status={admin!r}")
        assert admin == "up", (
            f"{port}: admin_status={admin!r}, expected 'up'.\n"
            f"Fix: sudo config interface startup {port}"
        )


def test_ethernet108_lldp_neighbor(ssh):
    """Ethernet108 (SR4 fiber) has an LLDP neighbor.

    Confirms the SR4 module is functioning and LLDP frames transit the fiber.
    Skipped if no neighbor found (physical connectivity issue, not a platform bug).
    """
    out, _, rc = ssh.run("show lldp neighbors", timeout=30)
    assert rc == 0, f"show lldp neighbors failed: {out}"

    if "Ethernet108" not in out:
        pytest.skip(
            "Ethernet108 has no LLDP neighbor — fiber may be disconnected "
            "or peer LLDP disabled. Skipping (not a platform failure)."
        )
    print(f"  Ethernet108: LLDP neighbor present")
```

- [ ] **Step 2: Run stage_13 against hardware**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_13_link/ -v
```
Expected: Existing tests pass (FEC already configured by deploy.py); new optical tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/stage_13_link/test_link.py
git commit -m "refactor: stage_13 remove configure_rsfec fixture, add optical port assertions"
```

---

### Task 14: stage_14_breakout — use Ethernet4 + module conftest.py

**Files:**
- Create: `tests/stage_14_breakout/conftest.py`
- Modify: `tests/stage_14_breakout/test_breakout.py`

Ethernet4 is the second QSFP parent (lanes 113–116, Port 2), with no connected hosts and not in the operational breakout set (Ethernet0/64/80). It is safe to break out and restore for testing.

The teardown polls `COUNTERS_PORT_NAME_MAP` (DB 2) until `Ethernet5`, `Ethernet6`, `Ethernet7` are absent — these sub-ports disappear when the 1×100G restore completes. Timeout 120s.

- [ ] **Step 1: Create stage_14_breakout/conftest.py**

```python
"""Stage 14 module fixture — break out Ethernet4 for test, restore after."""
import time
import pytest

BREAKOUT_TEST_PORT = "Ethernet4"
RESTORE_MODE = "1x100G[40G]"
BREAKOUT_MODE = "4x25G[10G]"
# Sub-ports that appear when Ethernet4 is broken out (excluding Ethernet4 itself,
# which is the first sub-port in both broken-out and restored states)
BREAKOUT_INDICATOR_SUBPORTS = ["Ethernet5", "Ethernet6", "Ethernet7"]
POLL_INTERVAL = 3
RESTORE_TIMEOUT = 120


@pytest.fixture(scope="module", autouse=True)
def stage14_breakout_fixture(ssh):
    """Break out Ethernet4 before tests; restore to 1x100G after.

    Teardown polls COUNTERS_PORT_NAME_MAP until Ethernet5/6/7 disappear,
    preventing a race condition with stage_15 if portmgrd hasn't finished.
    """
    # Ensure clean starting state
    ssh.run(
        f"sudo config interface breakout {BREAKOUT_TEST_PORT} '{RESTORE_MODE}' -y -f",
        timeout=60,
    )
    time.sleep(5)

    yield

    # Restore Ethernet4 to 1x100G
    ssh.run(
        f"sudo config interface breakout {BREAKOUT_TEST_PORT} '{RESTORE_MODE}' -y -f",
        timeout=60,
    )

    # Wait for sub-ports to disappear from COUNTERS_PORT_NAME_MAP
    deadline = time.time() + RESTORE_TIMEOUT
    while time.time() < deadline:
        out, _, _ = ssh.run(
            "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP", timeout=15
        )
        present = set(out.split())
        still_present = [p for p in BREAKOUT_INDICATOR_SUBPORTS if p in present]
        if not still_present:
            return
        time.sleep(POLL_INTERVAL)
    print(
        f"  [stage14 teardown] WARNING: sub-ports still in ASIC_DB after "
        f"{RESTORE_TIMEOUT}s: {still_present}"
    )
```

- [ ] **Step 2: Update BREAKOUT_PORT and SPEED_TEST_PORT in test_breakout.py**

In `tests/stage_14_breakout/test_breakout.py`, change:
```python
SPEED_TEST_PORT = "Ethernet0"
BREAKOUT_PORT = SPEED_TEST_PORT  # Same port used for breakout testing
```
to:
```python
# Ethernet4: Port 2, lanes 113-116, not in operational breakout, no connected hosts.
# Safe to modify speed and break out for testing.
SPEED_TEST_PORT = "Ethernet4"
BREAKOUT_PORT = SPEED_TEST_PORT
```

Also remove the `stage14_setup_teardown` session fixture from test_breakout.py (it is now replaced by the module fixture in conftest.py):
```python
@pytest.fixture(scope="session", autouse=True)
def stage14_setup_teardown(ssh):
    ...
```

And update `TestBreakoutCli._normalise_breakout` — change the restore target from `'1x100G[40G]'` to use `RESTORE_MODE` constant, and also update `TestBreakoutCli.test_breakout_cfg_current_mode` to skip Ethernet0/64/80 (these are operational breakout ports now):

```python
# In TestBreakoutCli.test_breakout_cfg_current_mode, replace the body:
def test_breakout_cfg_current_mode(self, ssh):
    """All non-operational ports in BREAKOUT_CFG show current mode 1x100G[40G]."""
    # Ethernet0, 64, 80 are operational breakout parents — skip them
    operational_breakout_parents = {"Ethernet0", "Ethernet64", "Ethernet80"}
    issues = []
    for port in PARENT_PORTS:
        if port in operational_breakout_parents:
            continue
        out, _, rc = ssh.run(
            f"redis-cli -n 4 hget 'BREAKOUT_CFG|{port}' brkout_mode",
            timeout=10,
        )
        mode = out.strip()
        if not mode:
            continue
        if mode != "1x100G[40G]":
            issues.append(f"  {port}: brkout_mode={mode!r}")
    if not issues:
        print(f"\n  All non-operational ports in 1x100G[40G] mode")
    assert not issues, (
        f"Unexpected breakout modes in BREAKOUT_CFG:\n"
        + "\n".join(issues)
    )
```

- [ ] **Step 3: Run stage_14 against hardware**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_14_breakout/ -v
```
Expected: All tests pass; Ethernet4 is restored to 1x100G on teardown.

- [ ] **Step 4: Commit**

```bash
git add tests/stage_14_breakout/conftest.py tests/stage_14_breakout/test_breakout.py
git commit -m "refactor: stage_14 use Ethernet4 as breakout test port, module-scoped fixture"
```

---

### Task 15: stage_15_autoneg_fec — module conftest.py with FEC teardown

**Files:**
- Create: `tests/stage_15_autoneg_fec/conftest.py`

The existing `stage15_setup_teardown` session fixture sets RS-FEC on connected ports and removes it on teardown. After deploy.py, RS-FEC is operational config — removing it in teardown would break the switch. Replace with a module fixture that saves/restores only `TEST_PORT`'s FEC.

`TEST_PORT = "Ethernet0"` — this is now a 4x25G sub-port but FEC rs is valid on 25G ports and the test just needs a port to toggle FEC on.

- [ ] **Step 1: Create stage_15_autoneg_fec/conftest.py**

```python
"""Stage 15 module fixture — capture and restore FEC on TEST_PORT."""
import time
import pytest

TEST_PORT = "Ethernet0"  # 4x25G sub-port, safe for FEC config-change tests


@pytest.fixture(scope="module", autouse=True)
def stage15_fec_fixture(ssh):
    """Save TEST_PORT FEC before tests; restore after.

    This prevents the per-test finally blocks in test_autoneg_fec.py from
    being the only cleanup path, and ensures the port is left in a known state
    even if tests abort mid-run.
    """
    # Read original FEC
    out, _, _ = ssh.run(
        f"redis-cli -n 4 hget 'PORT|{TEST_PORT}' fec", timeout=10
    )
    original_fec = out.strip() or "none"

    yield

    # Restore original FEC
    ssh.run(
        f"sudo config interface fec {TEST_PORT} {original_fec}", timeout=15
    )
    # Also clear any leftover autoneg/adv_speeds state
    ssh.run(
        f"redis-cli -n 4 hdel 'PORT|{TEST_PORT}' autoneg adv_speeds adv_interface_types",
        timeout=10,
    )
    time.sleep(1)
```

Also remove the `stage15_setup_teardown` session fixture from `test_autoneg_fec.py` (the one that sets RS-FEC on connected ports and removes it):

```python
# DELETE these lines from test_autoneg_fec.py:
@pytest.fixture(scope="session", autouse=True)
def stage15_setup_teardown(ssh):
    """Configure RS-FEC on connected ports so links are up; restore after."""
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    time.sleep(5)
    yield
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)
```

The `TestFecConnectedPorts` tests now just assert the pre-existing RS-FEC state (set by deploy.py).

- [ ] **Step 2: Run stage_15 against hardware**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_15_autoneg_fec/ -v
```
Expected: All tests pass. RS-FEC on connected ports remains after teardown.

- [ ] **Step 3: Commit**

```bash
git add tests/stage_15_autoneg_fec/conftest.py tests/stage_15_autoneg_fec/test_autoneg_fec.py
git commit -m "refactor: stage_15 module fixture replaces setup_teardown, no longer removes RS-FEC"
```

---

### Task 16: stage_16_portchannel — assert operational L2 PortChannel1

**Files:**
- Modify: `tests/stage_16_portchannel/test_portchannel.py`

This is a full rewrite. Key changes:
- Remove `stage16_setup_teardown` (was creating/deleting PortChannel1 + IP)
- Remove `TestLAGConnectivity` (no IP on PortChannel1 — ping not applicable)
- Remove `test_portchannel_ip_configured`
- Keep `TestTeamdFeature`, `TestPortChannelConfig` (without IP test), `TestLACPState`, `TestDBPropagation`, `TestASICDB`
- Add `TestLAGConnectivity_L2` — LLDP neighbor visible on Ethernet16 or Ethernet32
- Rewrite `TestLAGFailover` — use `teamdctl` state polling (not ping)

- [ ] **Step 1: Rewrite test_portchannel.py**

```python
"""Stage 16 — Port Channel / LAG (LACP) — operational state assertions.

Tests assert that PortChannel1 is already configured and operational,
as established by tools/deploy.py. PortChannel1 is L2-only (VLAN 999
access, no IP address).

Hardware topology:
  Hare Ethernet  | Rabbit Port  | Role
  ---------------|--------------|-----
  Ethernet16     | Et13/1       | PortChannel1 member
  Ethernet32     | Et14/1       | PortChannel1 member
  Ethernet48     | Et15/1       | standalone
  Ethernet112    | Et16/1       | standalone
"""

import re
import time
import pytest

PORTCHANNEL_NAME = "PortChannel1"
LAG_MEMBERS = ["Ethernet16", "Ethernet32"]
STANDALONE_PORTS = ["Ethernet48", "Ethernet112"]

TEAMDCTL_POLL_INTERVAL = 2
TEAMDCTL_FAILOVER_TIMEOUT = 10   # seconds for one member to deselect
TEAMDCTL_RECOVER_TIMEOUT = 30    # seconds for both members to reselect


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _portchannel_summary(ssh):
    """Parse 'show interfaces portchannel' into structured data."""
    out, err, rc = ssh.run("show interfaces portchannel", timeout=30)
    assert rc == 0, f"show interfaces portchannel failed (rc={rc}): {err}"
    result = {}
    for line in out.splitlines():
        m = re.match(r"\s*\d+\s+(\S+)\s+(LACP\(\S+\)\(\S+\))\s+(.*)", line)
        if m:
            name, protocol, ports_str = m.group(1), m.group(2), m.group(3).strip()
            members = {}
            for pm in re.finditer(r"(\S+)\(([SsDd\*])\)", ports_str):
                members[pm.group(1)] = pm.group(2)
            result[name] = {"protocol": protocol, "members": members}
    return result


def _teamdctl_members(ssh) -> dict:
    """Return {port_name: state_str} from teamdctl PortChannel1 state.

    state_str is 'current' when the member is selected and LACP-converged.
    """
    out, _, rc = ssh.run(f"teamdctl {PORTCHANNEL_NAME} state", timeout=15)
    if rc != 0:
        return {}
    result = {}
    current_port = None
    for line in out.splitlines():
        m = re.match(r"\s{4}(\S+):$", line)
        if m:
            current_port = m.group(1)
        if current_port and "runner.state:" in line:
            state = line.split(":", 1)[1].strip()
            result[current_port] = state
    return result


def _wait_for_member_state(ssh, port, expected_state, timeout):
    """Poll teamdctl until port reaches expected_state or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        members = _teamdctl_members(ssh)
        if members.get(port) == expected_state:
            return True
        time.sleep(TEAMDCTL_POLL_INTERVAL)
    return False


# ------------------------------------------------------------------
# teamd feature state
# ------------------------------------------------------------------

class TestTeamdFeature:

    def test_teamd_feature_enabled(self, ssh):
        """teamd feature is enabled in CONFIG_DB."""
        out, _, _ = ssh.run(
            "redis-cli -n 4 hget 'FEATURE|teamd' state", timeout=10
        )
        val = out.strip()
        print(f"  teamd feature state: {val!r}")
        assert val == "enabled", f"teamd feature state={val!r}; expected 'enabled'"

    def test_teamd_container_running(self, ssh):
        """teamd Docker container is running."""
        out, _, _ = ssh.run(
            "docker ps --format '{{.Names}}' --filter name=teamd", timeout=10
        )
        assert "teamd" in out, "teamd container is not running"


# ------------------------------------------------------------------
# PortChannel CONFIG_DB
# ------------------------------------------------------------------

class TestPortChannelConfig:

    def test_portchannel_exists_in_config_db(self, ssh):
        """PORTCHANNEL|PortChannel1 exists in CONFIG_DB."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 exists 'PORTCHANNEL|{PORTCHANNEL_NAME}'", timeout=10
        )
        assert out.strip() == "1", f"{PORTCHANNEL_NAME} not in CONFIG_DB"

    def test_portchannel_admin_up(self, ssh):
        """PortChannel1 admin_status is 'up' in CONFIG_DB."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORTCHANNEL|{PORTCHANNEL_NAME}' admin_status",
            timeout=10,
        )
        assert out.strip() == "up", f"admin_status={out.strip()!r}"

    def test_portchannel_has_no_ip(self, ssh):
        """PortChannel1 has no IP address (L2 VLAN 999 only)."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 keys 'PORTCHANNEL_INTERFACE|{PORTCHANNEL_NAME}|*'",
            timeout=10,
        )
        assert not out.strip(), (
            f"PortChannel1 has IP configured; expected L2-only: {out.strip()}"
        )

    def test_portchannel_members_in_config_db(self, ssh):
        """Both member ports are in PORTCHANNEL_MEMBER table."""
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 4 exists 'PORTCHANNEL_MEMBER|{PORTCHANNEL_NAME}|{port}'",
                timeout=10,
            )
            assert out.strip() == "1", f"{port} not a member of {PORTCHANNEL_NAME}"


# ------------------------------------------------------------------
# LACP negotiation state
# ------------------------------------------------------------------

class TestLACPState:

    def test_portchannel_lacp_active_up(self, ssh):
        """PortChannel1 shows LACP(A)(Up) in portchannel summary."""
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary
        protocol = summary[PORTCHANNEL_NAME]["protocol"]
        print(f"  {PORTCHANNEL_NAME} protocol: {protocol}")
        assert "Up" in protocol, f"protocol={protocol!r}; expected '(Up)'"

    def test_both_members_selected(self, ssh):
        """Both member ports show (S) = Selected in portchannel summary."""
        summary = _portchannel_summary(ssh)
        assert PORTCHANNEL_NAME in summary
        members = summary[PORTCHANNEL_NAME]["members"]
        for port in LAG_MEMBERS:
            assert port in members, f"{port} not listed in {PORTCHANNEL_NAME} members"
            assert members[port] == "S", f"{port} state={members[port]!r}; expected 'S'"

    def test_teamdctl_state_current(self, ssh):
        """teamdctl reports both ports as 'state: current' (LACP converged)."""
        out, err, rc = ssh.run(f"teamdctl {PORTCHANNEL_NAME} state", timeout=15)
        assert rc == 0, f"teamdctl state failed: {err}"
        assert "state: current" in out, (
            f"Expected 'state: current' in teamdctl output\nOutput:\n{out[:500]}"
        )
        assert "active: yes" in out, "teamd runner is not active"


# ------------------------------------------------------------------
# APP_DB and STATE_DB propagation
# ------------------------------------------------------------------

class TestDBPropagation:

    def test_lag_table_in_app_db(self, ssh):
        """LAG_TABLE:PortChannel1 exists in APP_DB with oper_status=up."""
        out, _, _ = ssh.run(
            f"redis-cli -n 0 hget 'LAG_TABLE:{PORTCHANNEL_NAME}' oper_status",
            timeout=10,
        )
        assert out.strip() == "up", f"APP_DB LAG oper_status={out.strip()!r}"

    def test_lag_member_table_in_app_db(self, ssh):
        """LAG_MEMBER_TABLE entries exist in APP_DB for both members."""
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 0 hget 'LAG_MEMBER_TABLE:{PORTCHANNEL_NAME}:{port}' status",
                timeout=10,
            )
            assert out.strip() == "enabled", (
                f"LAG_MEMBER {port} status={out.strip()!r} in APP_DB"
            )


# ------------------------------------------------------------------
# ASIC_DB LAG objects
# ------------------------------------------------------------------

class TestASICDB:

    def test_sai_lag_object_exists(self, ssh):
        """SAI_OBJECT_TYPE_LAG exists in ASIC_DB for PortChannel1."""
        oid_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget COUNTERS_LAG_NAME_MAP {PORTCHANNEL_NAME}",
            timeout=10,
        )
        oid = oid_out.strip()
        assert oid and oid.startswith("oid:"), (
            f"No OID for {PORTCHANNEL_NAME} in COUNTERS_LAG_NAME_MAP"
        )
        out, _, _ = ssh.run(
            f"redis-cli -n 1 exists 'ASIC_STATE:SAI_OBJECT_TYPE_LAG:{oid}'",
            timeout=10,
        )
        assert out.strip() == "1", f"SAI_OBJECT_TYPE_LAG:{oid} not in ASIC_DB"

    def test_sai_lag_member_objects_exist(self, ssh):
        """At least 2 SAI_OBJECT_TYPE_LAG_MEMBER entries in ASIC_DB."""
        out, _, _ = ssh.run(
            "redis-cli -n 1 keys 'ASIC_STATE:SAI_OBJECT_TYPE_LAG_MEMBER:*'",
            timeout=10,
        )
        members = [l for l in out.strip().splitlines() if l.strip()]
        assert len(members) >= 2, f"Expected >=2 LAG_MEMBER objects, found {len(members)}"


# ------------------------------------------------------------------
# L2 connectivity — LLDP over LAG
# ------------------------------------------------------------------

class TestLAGConnectivity:
    """Verify L2 connectivity over the port channel via LLDP.

    PortChannel1 is VLAN 999 access with no IP. LLDP is used as the
    L2 connectivity signal instead of ping.
    """

    def test_lldp_neighbor_on_lag_member(self, ssh):
        """LLDP neighbor (rabbit-lorax) is visible on Ethernet16 or Ethernet32."""
        out, _, rc = ssh.run("show lldp neighbors", timeout=30)
        assert rc == 0, f"show lldp neighbors failed: {out}"
        lag_lldp = [
            line for line in out.splitlines()
            if any(p in line for p in LAG_MEMBERS)
        ]
        assert lag_lldp, (
            "No LLDP neighbors found on Ethernet16 or Ethernet32.\n"
            "PortChannel1 is active but LLDP frames are not reaching the peer.\n"
            "Possible causes: LLDP container down, peer LLDP disabled."
        )
        print(f"  LLDP on LAG members: {len(lag_lldp)} entries found")


# ------------------------------------------------------------------
# LAG failover — teamdctl polling (no IP, no ping)
# ------------------------------------------------------------------

class TestLAGFailover:
    """Verify LAG survives a single member link failure.

    Uses teamdctl state polling as the convergence signal.
    No ping because PortChannel1 carries no IP (L2 VLAN 999).
    """

    @pytest.fixture(autouse=True)
    def _restore_lag_members(self, ssh):
        """Ensure all LAG member ports are admin-up after the test."""
        yield
        for port in LAG_MEMBERS:
            out, _, _ = ssh.run(
                f"redis-cli -n 4 hget 'PORT|{port}' admin_status", timeout=10
            )
            if out.strip() != "up":
                ssh.run(f"sudo config interface startup {port}", timeout=15)
                time.sleep(2)

    def test_failover_and_recovery(self, ssh):
        """Shut Ethernet16, assert Ethernet32 stays selected; restore, assert both selected."""
        fail_port = "Ethernet16"
        survive_port = "Ethernet32"

        # Phase 1: shut down fail_port
        _, _, rc = ssh.run(f"sudo config interface shutdown {fail_port}", timeout=15)
        assert rc == 0, f"Failed to shutdown {fail_port}"

        # Wait for survive_port to remain 'current' in teamdctl (within 10s)
        ok = _wait_for_member_state(ssh, survive_port, "current", TEAMDCTL_FAILOVER_TIMEOUT)
        members = _teamdctl_members(ssh)
        print(f"  After shutdown {fail_port}: teamdctl members={members}")
        assert ok, (
            f"{survive_port} did not remain 'current' within {TEAMDCTL_FAILOVER_TIMEOUT}s "
            f"after shutting {fail_port}. Members: {members}"
        )

        # Also verify PortChannel is still up
        summary = _portchannel_summary(ssh)
        assert "Up" in summary.get(PORTCHANNEL_NAME, {}).get("protocol", ""), (
            f"{PORTCHANNEL_NAME} went down after shutting {fail_port}"
        )

        # Phase 2: restore fail_port
        _, _, rc = ssh.run(f"sudo config interface startup {fail_port}", timeout=15)
        assert rc == 0, f"Failed to startup {fail_port}"

        # Wait for both members to return to 'current' (within 30s)
        deadline = time.time() + TEAMDCTL_RECOVER_TIMEOUT
        both_selected = False
        while time.time() < deadline:
            members = _teamdctl_members(ssh)
            if all(members.get(p) == "current" for p in LAG_MEMBERS):
                both_selected = True
                break
            time.sleep(TEAMDCTL_POLL_INTERVAL)

        members = _teamdctl_members(ssh)
        print(f"  After recovery: teamdctl members={members}")
        assert both_selected, (
            f"Both members did not return to 'current' within {TEAMDCTL_RECOVER_TIMEOUT}s. "
            f"Members: {members}"
        )


# ------------------------------------------------------------------
# Standalone ports unaffected
# ------------------------------------------------------------------

class TestStandalonePortsUnaffected:

    def test_standalone_ports_still_up(self, ssh):
        """Ethernet48 and Ethernet112 (not in LAG) remain oper=up."""
        out, _, rc = ssh.run("show interfaces status", timeout=30)
        assert rc == 0
        for port in STANDALONE_PORTS:
            m = re.search(rf"\s*{port}\s+.*?\s+(up|down)\s+(up|down)", out)
            assert m, f"Could not parse status for {port}"
            oper = m.group(1)
            print(f"  {port}: oper={oper}")
            assert oper == "up", f"{port} oper={oper!r} — standalone port should be up"
```

- [ ] **Step 2: Run stage_16 against hardware**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_16_portchannel/ -v
```
Expected: All tests pass. Failover test shuts Ethernet16, verifies Ethernet32 stays selected, re-enables Ethernet16.

- [ ] **Step 3: Commit**

```bash
git add tests/stage_16_portchannel/test_portchannel.py
git commit -m "refactor: stage_16 assert operational PortChannel1, L2-only failover via teamdctl"
```

---

### Task 17: stage_21_lpmode — module conftest.py with LP_MODE restore

**Files:**
- Create: `tests/stage_21_lpmode/conftest.py`

Capture LP_MODE state for all present ports before tests; restore after. The existing test_lpmode.py has per-test teardown (`_restore()` helpers), but module-scoped restore is the safety net.

- [ ] **Step 1: Create stage_21_lpmode/conftest.py**

```python
"""Stage 21 module fixture — capture and restore LP_MODE state."""
import time
import pytest

NUM_PORTS = 32
RUN_DIR = "/run/wedge100s"


def _read_lpmode_states(ssh) -> dict:
    """Return {idx: '0'|'1'} for all ports that have an lpmode state file."""
    states = {}
    for idx in range(NUM_PORTS):
        out, _, rc = ssh.run(
            f"cat {RUN_DIR}/sfp_{idx}_lpmode 2>/dev/null", timeout=5
        )
        if rc == 0 and out.strip() in ("0", "1"):
            states[idx] = out.strip()
    return states


@pytest.fixture(scope="module", autouse=True)
def stage21_lpmode_fixture(ssh):
    """Save LP_MODE state before tests; restore after.

    This ensures optical TX lasers are re-enabled even if a test
    asserts lpmode=1 and fails before its own teardown runs.
    """
    original_states = _read_lpmode_states(ssh)

    yield

    # Restore: write _lpmode_req files and trigger daemon
    for idx, state in original_states.items():
        ssh.run(
            f"echo {state} > {RUN_DIR}/sfp_{idx}_lpmode_req", timeout=5
        )
    if original_states:
        ssh.run("wedge100s-i2c-daemon poll-presence", timeout=30)
        time.sleep(1)
```

- [ ] **Step 2: Run stage_21 against hardware**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_21_lpmode/ -v
```
Expected: All tests pass, LP_MODE state is restored after tests.

- [ ] **Step 3: Commit**

```bash
git add tests/stage_21_lpmode/conftest.py
git commit -m "feat: stage_21 module fixture restores LP_MODE state after tests"
```

---

### Task 18: cleanup, target.cfg.example, STAGED_PHASES.md

**Files:**
- Delete: `tests/lib/prepost.py`
- Delete: `tests/fixtures/clean_boot.json`
- Modify: `tests/target.cfg.example`
- Modify: `tests/STAGED_PHASES.md`

- [ ] **Step 1: Verify no remaining imports of prepost.py**

```bash
grep -r "prepost" /export/sonic/sonic-buildimage.claude/tests/ --include="*.py"
```
Expected: zero results (all imports removed in Tasks 11–12).

- [ ] **Step 2: Delete prepost.py and clean_boot.json**

```bash
rm /export/sonic/sonic-buildimage.claude/tests/lib/prepost.py
rm /export/sonic/sonic-buildimage.claude/tests/fixtures/clean_boot.json
```

- [ ] **Step 3: Update target.cfg.example — add [hosts] section**

Append to `tests/target.cfg.example`:

```ini

[hosts]
# SSH credentials for test hosts connected to breakout ports.
# Used by stage_22_host_traffic (PENDING).
ssh_user = flax
key_file = ~/.ssh/id_rsa
```

- [ ] **Step 4: Update STAGED_PHASES.md**

Update Phase 00 status:
```markdown
## Phase 00: Pre-test Preconditions
**Status: COMPLETE (refactored)**
- Removed save/reload/restore model
- Now a read-only operational audit: verifies deploy.py has been run
- Checks: mgmt VRF, breakout sub-ports, PortChannel1, VLANs, FEC config
```

Update Phase 16 status:
```markdown
## Phase 16: Port Channel / LAG
**Status: COMPLETE (refactored)**
- L2-only mode: PortChannel1 on VLAN 999, no IP address
- Failover test uses teamdctl state polling (not ping)
- LLDP used as L2 connectivity signal
```

Add Phase 22 at the end:
```markdown
## Phase 22: Host Traffic Throughput
**Status: PENDING**
- Purpose: L2 throughput between test hosts via VLAN 10
- Prerequisites: tools/deploy.py run, all 6 hosts SSH-reachable
- Test pairs: intra-QSFP (Eth0↔Eth1, Eth66↔Eth67, Eth80↔Eth81) + cross-QSFP
- Tool: iperf3; assert ≥20 Gbps for 25G ports, ≥8 Gbps for 10G (Ethernet64 group)
- target.cfg gains [hosts] section with ssh_user=flax
- Deferred to next implementation cycle
```

- [ ] **Step 5: Run full test suite to verify no regressions**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 run_tests.py -- --no-header -q 2>&1 | tail -40
```
Expected: All stages run, stage_00 passes (deploy.py state present), no abort.

- [ ] **Step 6: Commit**

```bash
git add -u  # picks up deletions
git add tests/target.cfg.example tests/STAGED_PHASES.md
git commit -m "chore: remove prepost.py/clean_boot.json, update target.cfg.example and STAGED_PHASES"
```

---

## Verification Checklist

Before claiming complete, verify:

- [ ] `python3 tools/deploy.py --dry-run` exits 0 with "no changes needed" for all tasks
- [ ] `python3 tools/deploy.py` runs without error on a fresh install (idempotent re-runs also work)
- [ ] `python3 -m pytest tools/ -v` — all unit tests pass (no hardware needed)
- [ ] `python3 -m pytest tests/test_run_tests_unit.py -v` — all 9 unit tests pass
- [ ] `python3 tests/run_tests.py` — full suite runs; stage_00 passes; stage_nn passes
- [ ] `grep -r "prepost" tests/ --include="*.py"` — zero results
- [ ] `grep -r "clean_boot" tests/ --include="*.py"` — zero results
- [ ] `grep -r "save_and_reload_clean\|restore_user_config" tests/` — zero results
