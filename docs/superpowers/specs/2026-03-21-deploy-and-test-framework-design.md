# Deploy Tool and Test Framework Redesign
**Date:** 2026-03-21
**Status:** Approved for implementation

---

## 1. Goals

Two deliverables:

1. **`tools/deploy.py`** — idempotent, role-based Python tool that takes a freshly-installed SONiC switch from "DHCP only" to fully operational L2 test platform. Single command, run once per fresh install or whenever the operational config drifts. Designed for future conversion to Ansible custom modules.

2. **Refactored test framework** (`tests/`) — assertive session checks replace the current save/reload/restore model. Tests run against the operational state established by `deploy.py`. Fast, safe, never touches mgmt VRF or SSH config.

A third deliverable is designed but deferred: `stage_22_host_traffic` throughput test between servers connected to breakout ports.

---

## 2. Hardware Topology

Defined in `tools/topology.json` (data, not code). The Python tasks read this file; changing topology means editing JSON, not Python.

### 2.1 Breakout ports → test hosts

Three QSFP28 ports broken out to 4×25G. Six lanes are connected; six are unused (wired but not populated).

| QSFP parent | Sub-port | Host | Mgmt IP | Test IP (VLAN 10) |
|---|---|---|---|---|
| Ethernet0 | Ethernet0 | test-et6b3 | 192.168.88.243 | 10.0.10.243 |
| Ethernet0 | Ethernet1 | test-et8b4 | 192.168.88.232 | 10.0.10.232 |
| Ethernet64 | Ethernet66 | test-et7b3 | 192.168.88.237 | 10.0.10.237 |
| Ethernet64 | Ethernet67 | test-et25b1 | 192.168.88.242 | 10.0.10.242 |
| Ethernet80 | Ethernet80 | test-et7b1 | 192.168.88.225 | 10.0.10.225 |
| Ethernet80 | Ethernet81 | test-et6b2 | 192.168.88.241 | 10.0.10.241 |

Unused sub-ports (Ethernet2, Ethernet3, Ethernet64, Ethernet65, Ethernet82, Ethernet83) are admin-up with no expected link.

### 2.2 Uplinks → Arista rabbit-lorax

| SONiC port | Arista port | Role |
|---|---|---|
| Ethernet16 | Et13/1 | PortChannel1 member |
| Ethernet32 | Et14/1 | PortChannel1 member |
| Ethernet48 | Et15/1 | standalone |
| Ethernet112 | Et16/1 | standalone |

PortChannel1 is VLAN 999 access on both sides (L2 only, no IP). Arista side configured as `switchport access vlan 999`.

### 2.3 Optical ports

| Port | Module | FEC |
|---|---|---|
| Ethernet100 | Arista QSFP28-SR4-100G | rs |
| Ethernet104 | Arista QSFP28-LR4-100G | rs |
| Ethernet108 | Arista QSFP28-SR4-100G | rs |
| Ethernet116 | ColorChip CWDM4 | rs |

### 2.4 Management

- eth0: DHCP (192.168.88.12), mgmt VRF `mgmt`, table 1000
- SSH: `ip vrf exec mgmt /usr/sbin/sshd` via systemd drop-in
- Gateway: 192.168.88.2

---

## 3. Deploy Tool

### 3.1 File structure

```
tools/
├── deploy.py              # CLI entry point
├── topology.json          # Device topology (all data lives here)
└── tasks/
    ├── __init__.py
    ├── base.py            # ConfigTask base class + Change dataclass
    ├── mgmt_vrf.py        # MgmtVrfTask
    ├── breakout.py        # BreakoutTask
    ├── vlans.py           # VlanTask
    ├── portchannel.py     # PortChannelTask
    └── optical.py         # OpticalTask
```

`tests/lib/ssh_client.py` is imported directly by `deploy.py` — no duplication. If it needs to move to a top-level `lib/` in future, that is a mechanical rename.

### 3.2 ConfigTask base class

```python
@dataclass
class Change:
    item: str       # human-readable description of what changes
    current: str    # observed current value
    desired: str    # target value
    cmd: str        # SONiC CLI command to apply this change

class ConfigTask:
    def __init__(self, ssh, topology: dict): ...
    def check(self) -> list[Change]: ...    # query only, never modifies state
    def apply(self, changes: list[Change]) -> None: ...
    def verify(self) -> bool: ...          # post-apply assertion
```

`deploy.py` drives each task: `check()` → print changes → `apply()` → `verify()`. With `--dry-run`, `apply()` is never called.

### 3.3 Task responsibilities

**MgmtVrfTask** (runs first; reconnects SSH after service restart):
1. Assert `ip vrf show` contains `mgmt`; create VRF + bring up if missing
2. Assert `ip link show eth0` shows `master mgmt`; move eth0 into VRF if missing
3. Assert default route exists in mgmt routing table; add if missing
4. Assert `MGMT_VRF_CONFIG|vrf_global.mgmtVrfEnabled == true` in CONFIG_DB; set if missing
5. Assert SSH drop-in at `/etc/systemd/system/ssh.service.d/sonic.conf` is correct. "Correct" means the file exists and contains `ExecStart=/usr/bin/ip vrf exec mgmt /usr/sbin/sshd -D $SSHD_OPTS`. If the file is missing or that exact `ExecStart` line is absent, write the file, then `systemctl daemon-reload && systemctl restart ssh`.
6. After SSH restart: reconnect with 2 s retry interval, 30 s maximum. If reconnect fails after 30 s, `deploy.py` exits with a clear error ("SSH did not recover after VRF restart — check drop-in manually") without touching any further tasks.

**BreakoutTask** (batched, single wait):
1. `check()` reads `BREAKOUT_CFG` in CONFIG_DB for each parent port in `topology.json`
2. `apply()` issues all needed `config interface breakout <parent> 4x25G` commands in a single pass — no waiting between them
3. `verify()` polls `redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP` until all expected sub-ports appear (not `show interfaces status`, which lags behind ASIC_DB convergence). Poll interval 3 s, timeout 120 s. Verify checks all twelve expected sub-ports regardless of how many breakout commands were issued — handles the partial re-run case where CONFIG_DB was already updated but portmgrd had not completed.

**VlanTask**: Create VLANs 10 and 999; add all breakout sub-ports to VLAN 10 as access members; add PortChannel1 to VLAN 999 as access member.

**PortChannelTask**: Create PortChannel1 with `lacp_mode: active`; add Ethernet16 and Ethernet32 as members. `check()` also detects if PortChannel1 has a pre-existing IP address (queried via `redis-cli -n 4 HGETALL 'INTERFACE|PortChannel1'`) — if an IP is found, `apply()` removes it with `config interface ip remove PortChannel1 <ip>` before the VLAN membership step in VlanTask runs.

**OpticalTask**: Set FEC mode per `optical_ports` in topology.json; assert admin-up.

**Execution order in deploy.py:**
```
MgmtVrfTask → BreakoutTask → PortChannelTask → VlanTask → OpticalTask → config save -y
```
PortChannelTask runs before VlanTask because `VlanTask` adds PortChannel1 as a VLAN 999 member and SONiC rejects `config vlan member add` for a portchannel that does not yet exist in CONFIG_DB.

`config save -y` is called once at the end, not after each task.

### 3.4 topology.json schema

After 4×25G breakout, SONiC retains the parent port name as the first sub-port. QSFP parent `Ethernet0` becomes sub-ports `Ethernet0`, `Ethernet1`, `Ethernet2`, `Ethernet3` — `Ethernet0` in `vlans[].members` therefore refers to the valid first sub-port, not the pre-breakout parent.

VLAN 10 membership is the authoritative source for which ports carry host traffic. The `hosts` array references ports by the same sub-port names; `deploy.py` validates at startup that every `hosts[].port` is present in the VLAN 10 member list and exits with an error if they diverge.

```json
{
  "device": {
    "mgmt_gateway": "192.168.88.2",
    "mgmt_vrf": "mgmt"
  },
  "breakout_ports": [
    {"parent": "Ethernet0",  "mode": "4x25G"},
    {"parent": "Ethernet64", "mode": "4x25G"},
    {"parent": "Ethernet80", "mode": "4x25G"}
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

### 3.5 CLI interface

```
tools/deploy.py [--dry-run] [--target-cfg tests/target.cfg] [--topology tools/topology.json]
                [--task mgmt_vrf|breakout|vlans|portchannel|optical]  # run single task

--dry-run      call check() on all tasks, print Change list, exit without applying
--task <name>  run only the named task (useful for targeted re-runs)
```

---

## 4. Test Framework Refactor

### 4.1 Session lifecycle (conftest.py)

Replaces `lib/prepost.py` entirely. No `config reload`, no snapshot, no restore.

**Session start** — assertive checks in order, stopping on first failure:

| Check | How | Failure action |
|---|---|---|
| SSH alive and pmon running | `systemctl is-active pmon` | `pytest.exit(2, "SSH or pmon unreachable")` |
| `ip vrf show` contains `mgmt` | shell command | `pytest.exit(3, "mgmt VRF missing — run tools/deploy.py")` |
| All 12 breakout sub-ports in COUNTERS_PORT_NAME_MAP | `redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP` | `pytest.exit(3, "breakout not configured — run tools/deploy.py")` |
| PortChannel1 present in CONFIG_DB | `redis-cli -n 4 EXISTS 'PORTCHANNEL\|PortChannel1'` | `pytest.exit(3, "PortChannel1 missing — run tools/deploy.py")` |
| VLAN 10 and VLAN 999 present in CONFIG_DB | `redis-cli -n 4 EXISTS 'VLAN\|Vlan10'` etc. | `pytest.exit(3, "VLANs missing — run tools/deploy.py")` |

Breakout is checked via `COUNTERS_PORT_NAME_MAP` (ASIC_DB, DB 2) rather than `show interfaces status` to avoid false failures during portmgrd boot convergence.

**Session end** — health check only:
- SSH still alive
- pmon still running
- No Docker containers in `Exited` or `Error` state

### 4.2 run_tests.py stage-level abort

`run_tests.py` invokes pytest **once per stage** via subprocess, passing `--junitxml=<tmpfile>`. After each stage completes, it parses the XML `<testsuite>` element:

- If `tests == 0`: stage had no collectible tests — treat as skip, continue.
- If `tests > 0` and `passed == 0` and `skipped < tests`: stage completely failed — print message and abort remaining stages.
- If `tests > 0` and `passed == 0` and `skipped == tests`: all tests were skipped (e.g. host stage when hosts unreachable) — continue.
- Otherwise: some tests passed, some failed — continue (individual failures are non-fatal).

`xfail` outcomes count as `passed` for this determination. Stages where every test is `xfail` are not treated as complete failures.

**`--no-prepost` flag**: stage_00 and stage_nn are not injected. The abort logic still applies to all remaining stages. stage_00 is subject to the abort rule: if all its audit tests fail, the run stops (this is equivalent to "deploy was not run").

**`--report` mode**: unchanged by this refactor. It uses a separate code path (`_run_report()`) that does not invoke pytest and is not affected by the per-stage subprocess change.

**`stage_00_pretest`**: subject to the abort rule. If all its tests fail (deploy was not run, or switch is misconfigured), `run_tests.py` aborts before running any functional stages.

### 4.3 Stage changes

**stage_00_pretest** — becomes a read-only operational state audit:
- Remove: `save_and_reload_clean()`, suite marker file, `clean_boot.json` upload
- Keep as assertions: ports admin-up, PortChannel1 active, VLANs exist, pmon running
- Failure = "run `tools/deploy.py` before running tests"

**stage_nn_posttest** — becomes a health check:
- Remove: `restore_user_config()`, config save, snapshot reload
- Keep: pmon running, SSH alive, no crashed containers

**stage_16_portchannel** — no longer creates/deletes PortChannel1:
- Assert existing PortChannel1 is up, LACP active, both members selected via `teamdctl PortChannel1 state`
- Verify L2 connectivity over LAG: assert rabbit-lorax appears as an LLDP neighbor on Ethernet16 or Ethernet32 via `show lldp neighbors`. LLDP is used instead of ping because PortChannel1 carries no IP (VLAN 999, L2 only).
- Failover sub-test: shut down Ethernet16 (`config interface shutdown Ethernet16`), assert Ethernet32 remains `selected` in teamdctl state within 10 s, re-enable Ethernet16, assert both members return to `selected` within 30 s. Uses teamdctl state polling rather than LLDP (LLDP hold-timer convergence is 30–60 s and is impractical as a failover signal).
- No teardown needed — Ethernet16 is re-enabled within the test itself.

**stage_13_link** — assert Ethernet108 is up (RS-FEC, confirmed via LLDP); assert optical port admin config is correct for all four ports.

**lib/prepost.py** — deleted.

### 4.4 Per-stage teardown (pytest module fixtures with yield)

Three stages make transient config changes and restore them via `yield`-based module-scoped fixtures in their `conftest.py`:

| Stage | Transient change | Teardown action |
|---|---|---|
| `stage_14_breakout` | Break out Ethernet4 (unused QSFP) to 4×25G | Restore Ethernet4 to 1×100G, wait for pmon |
| `stage_15_autoneg_fec` | Toggle FEC on a connected port | Restore original FEC mode |
| `stage_21_lpmode` | Assert/deassert LP_MODE on installed modules | Restore LP_MODE state read before test |

Ethernet4 is chosen for the breakout test because it is not an operational breakout parent and has no connected hosts — breaking it out and restoring it does not disrupt traffic.

The `stage_14_breakout` module teardown polls `COUNTERS_PORT_NAME_MAP` (DB 2) until `Ethernet5`, `Ethernet6`, and `Ethernet7` are absent — these sub-ports disappear when the 1×100G restore completes. (`Ethernet4` itself is present in both the broken-out and restored states as the first sub-port or the restored parent, so its presence is not a meaningful signal.) Timeout 120 s. This prevents a race condition with `stage_15_autoneg_fec` if both stages run back-to-back and portmgrd has not finished processing the restore.

---

## 5. Future: stage_22_host_traffic

Designed here, not implemented in this cycle. Added to STAGED_PHASES.md as PENDING.

**Purpose:** Measure L2 throughput between test hosts via SONiC VLAN 10. Validates that the switch fabric, breakout ports, and VLAN forwarding deliver expected 25G line rate.

**Prerequisites:** `tools/deploy.py` has run (VLAN 10 configured, breakout ports up, hosts reachable at test IPs).

**Session fixture:** Verify all six host mgmt IPs are SSH-reachable; skip entire stage with `pytest.skip` if any are unreachable rather than failing.

**target.cfg** gains a `[hosts]` section:
```ini
[hosts]
ssh_user = flax
key_file = ~/.ssh/id_rsa
```

**Test pairs:**

| Client | Server | Tests |
|---|---|---|
| test-et6b3 (Eth0) | test-et8b4 (Eth1) | intra-QSFP0 forwarding |
| test-et7b3 (Eth66) | test-et25b1 (Eth67) | intra-QSFP16 forwarding |
| test-et7b1 (Eth80) | test-et6b2 (Eth81) | intra-QSFP20 forwarding |
| test-et6b3 (Eth0) | test-et7b1 (Eth80) | cross-QSFP fabric path |

Each test: start `iperf3 -s` on server host, run `iperf3 -c <test_ip> -t 10` on client host via SSH, assert throughput ≥ 20 Gbps (80% of 25G line rate), and assert throughput ≥ 8Gbps (80% of 10G line rate for breakouts on Ethernet64 -- which is 4x10G breakout) and minimal retransmits.

No teardown needed — iperf3 is stateless.

---

## 6. Files Created / Modified

| File | Action |
|---|---|
| `tools/deploy.py` | **new** |
| `tools/topology.json` | **new** |
| `tools/tasks/__init__.py` | **new** |
| `tools/tasks/base.py` | **new** |
| `tools/tasks/mgmt_vrf.py` | **new** |
| `tools/tasks/breakout.py` | **new** |
| `tools/tasks/vlans.py` | **new** |
| `tools/tasks/portchannel.py` | **new** |
| `tools/tasks/optical.py` | **new** |
| `tests/conftest.py` | **modified** — session fixture replaces prepost |
| `tests/run_tests.py` | **modified** — stage-level abort logic |
| `tests/stage_00_pretest/test_pretest.py` | **modified** — remove reload, become audit |
| `tests/stage_nn_posttest/test_posttest.py` | **modified** — remove restore, become health check |
| `tests/stage_13_link/test_link.py` | **modified** — assert operational state |
| `tests/stage_14_breakout/conftest.py` | **new** — module fixture with teardown |
| `tests/stage_14_breakout/test_breakout.py` | **modified** — use Ethernet4, not Ethernet0 |
| `tests/stage_15_autoneg_fec/conftest.py` | **new** — module fixture with teardown |
| `tests/stage_16_portchannel/test_portchannel.py` | **modified** — assert operational PortChannel1 |
| `tests/stage_21_lpmode/conftest.py` | **new** — module fixture with teardown |
| `tests/lib/prepost.py` | **deleted** |
| `tests/fixtures/clean_boot.json` | **deleted** |
| `tests/target.cfg.example` | **modified** — add `[hosts]` section |
| `tests/STAGED_PHASES.md` | **modified** — add Phase 22 as PENDING |
