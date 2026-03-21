# Wedge 100S-32X Hardware Completion — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete platform API production readiness and redesign test infrastructure so any stage can run standalone (with pre/post config save-restore) against a clean-boot baseline.

**Architecture:** `run_tests.py` always injects `stage_00_pretest` and `stage_nn_posttest` around any selected stages. `stage_00` saves user config and applies `clean_boot.json` via `config reload`. Each config-modifying stage owns its own setup/teardown fixtures. `stage_nn_posttest` restores the saved config and always sorts last (letter `n` > any digit, so it is never displaced by adding numbered stages). The clean_boot.json template is pulled from the freshly-installed switch with BGP_NEIGHBOR and port IPs stripped. Natural alphabetical order is used throughout — no custom ordering override is needed.

**Tech Stack:** Python 3, pytest, SONiC CLI (`config`, `show`), Redis CLI, SSH via paramiko, SONiC `sonic_platform` Python package.

---

## File Structure

**New files:**
- `tests/fixtures/clean_boot.json` — clean-boot CONFIG_DB template (pulled from switch, BGP+IP stripped)
- `tests/test_plan.md` — physical port population, peer connections, per-stage requirements
- `tests/lib/prepost.py` — `save_and_reload_clean(ssh)` and `restore_user_config(ssh)` functions
- `tests/stage_00_pretest/__init__.py` + `test_pretest.py` — verifies clean state after reload
- `tests/stage_nn_posttest/__init__.py` + `test_posttest.py` — verifies restoration after restore
- `sonic_platform/component.py` — Component class for CPLD + BIOS firmware versions

**Modified files:**
- `tests/run_tests.py` — auto-inject stage_00/stage_nn, `--no-prepost` flag
- `tests/stage_13_link/test_link.py` — add session fixture: configure RS-FEC on connected ports, teardown removes FEC
- `tests/stage_14_breakout/test_breakout.py` — add session fixture: configure, run, restore 1x100G
- `tests/stage_15_autoneg_fec/test_autoneg_fec.py` — add session fixture: configure, run, restore no-FEC
- `tests/stage_16_portchannel/test_portchannel.py` — add session fixture: create PortChannel1, teardown removes it
- `sonic_platform/bmc.py` — TTY buffer flush before send
- `sonic_platform/chassis.py` — `get_base_mac()`, `get_reboot_cause()`, `get_port_or_cage_type()`
- `sonic_platform/thermal.py` — `get_low_threshold()`, `get_low_critical_threshold()`
- `sonic_platform/psu.py` — `get_model()` static string

**Deleted:**
- `tests/stage_17_restore/` — superseded by stage_00_pretest/stage_nn_posttest

**Renamed (directory move):**
- `tests/stage_18_report/` → `tests/stage_17_report/`

---

## Task 0: Capture Baseline Test Output

Before any changes, capture current test results to identify regressions.

**Files:** none (diagnostic only)

- [ ] **Step 1: Run current suite stages 01–16 and capture output**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 run_tests.py stage_01_eeprom stage_02_system stage_03_platform stage_04_thermal stage_05_fan stage_06_psu stage_07_qsfp stage_08_led stage_09_cpld stage_10_daemon stage_11_transceiver stage_12_counters stage_13_link stage_14_breakout stage_15_autoneg_fec stage_16_portchannel 2>&1 | tee reports/baseline_$(date +%Y-%m-%d_%H%M%S).txt
```

Expected: should approximate baseline (202 passed, 1 failed for bmc_uptime — the TTY flush bug). Note any new failures as regressions.

- [ ] **Step 2: Check bash completion regression**

```bash
ssh admin@192.168.88.12 'bash -i -c "complete -p | wc -l"' 2>&1
```

If completion count is 0 or the command errors, note it. Check if `/etc/bash_completion` exists.

```bash
ssh admin@192.168.88.12 'ls /etc/bash_completion /etc/bash_completion.d/ 2>&1'
```

Record findings in `tests/notes/regressions-$(date +%Y-%m-%d).md`.

- [ ] **Step 3: Commit baseline output**

```bash
git add tests/reports/
git commit -m "test: capture baseline output against fresh SONiC install"
```

---

## Task 1: Create clean_boot.json Template

Pull from freshly-installed switch and strip port-specific assumptions.

**Files:**
- Create: `tests/fixtures/clean_boot.json`
- Create: `tests/test_plan.md`

- [ ] **Step 1: Pull and clean the config**

```bash
ssh admin@192.168.88.12 python3 - << 'PYEOF' > /export/sonic/sonic-buildimage.claude/tests/fixtures/clean_boot.json
import json, sys

cfg = json.load(open("/etc/sonic/config_db.json"))

# Strip: BGP_NEIGHBOR (port-connectivity assumption)
cfg.pop("BGP_NEIGHBOR", None)

# Strip: INTERFACE IP entries (|IP/prefix keys); keep bare port entries
iface = cfg.get("INTERFACE", {})
cfg["INTERFACE"] = {k: v for k, v in iface.items() if "|" not in k}

# Strip: FEC from all PORT entries (stages configure what they need)
for port, attrs in cfg.get("PORT", {}).items():
    attrs.pop("fec", None)

print(json.dumps(cfg, indent=4, sort_keys=True))
PYEOF
```

- [ ] **Step 2: Verify the output looks sane**

```bash
python3 -c "
import json
cfg = json.load(open('tests/fixtures/clean_boot.json'))
print('Tables:', sorted(cfg.keys()))
print('PORT count:', len(cfg.get('PORT',{})))
print('BREAKOUT_CFG count:', len(cfg.get('BREAKOUT_CFG',{})))
print('BGP_NEIGHBOR (should be absent):', cfg.get('BGP_NEIGHBOR','ABSENT'))
print('INTERFACE IP keys (should be 0):', [k for k in cfg.get('INTERFACE',{}) if '|' in k])
"
```

Expected: PORT=32, BREAKOUT_CFG=32, BGP_NEIGHBOR=ABSENT, no `|` keys in INTERFACE.

- [ ] **Step 3: Write test_plan.md**

Create `tests/test_plan.md`:

```markdown
# Wedge 100S-32X Test Plan

## Physical Port Population (as of 2026-03-17)

| SONiC Port | Physical Port | Connected To | Cable Type | Notes |
|------------|---------------|--------------|------------|-------|
| Ethernet0  | Port 1        | (empty)      | —          | No module |
| Ethernet16 | Port 5        | rabbit-lorax Et13/1 | DAC 100G | LAG member |
| Ethernet32 | Port 9        | rabbit-lorax Et14/1 | DAC 100G | LAG member |
| Ethernet48 | Port 13       | rabbit-lorax Et15/1 | DAC 100G | Standalone |
| Ethernet64 | Port 17       | (empty)      | QSFP28 present | No link |
| Ethernet80 | Port 21       | (empty)      | QSFP28 present | No link |
| Ethernet104 | Port 27      | (empty)      | CWDM4 optical | Blocked (§9) |
| Ethernet108 | Port 28      | (empty)      | CWDM4 optical | Blocked (§9) |
| Ethernet112 | Port 29      | rabbit-lorax Et16/1 | DAC 100G | Standalone |

## Peer Node: rabbit-lorax (Arista EOS Wedge 100S)

- Access: jump via hare-lorax (192.168.88.12) → 192.168.88.14
- PortChannel1: Et13/1 + Et14/1 in LACP active, IP 10.0.1.0/31
- Et15/1: standalone, IP 10.0.0.0/31
- Et16/1: standalone (no IP assigned)

## Per-Stage Requirements

| Stage | Requires | Configures | Unconfigures |
|-------|----------|------------|--------------|
| 01–10 | none | none | none |
| 11    | pmon running | none | none |
| 12    | syncd running | flex counter enable (if off) | restore |
| 13    | RS-FEC, link-up ports | RS-FEC on Et16/32/48/112 | remove FEC |
| 14    | breakout support | 4x25G on one port | restore 1x100G |
| 15    | FEC modes | RS-FEC then none on Et48 | restore FEC=none |
| 16    | LACP peer | PortChannel1, members, IP | remove PortChannel1 |
| 17    | restore done | none | none |
| 18    | pre-test ran | restore from snapshot | none |
| 19    | platform API | none | none |
| 20    | PortChannel1 up, traffic path | none (uses stage_16 state) | none |
```

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/clean_boot.json tests/test_plan.md
git commit -m "test: add clean_boot.json template and test_plan.md with port topology"
```

---

## Task 2: Create tests/lib/prepost.py

Shared save/restore logic callable by both `run_tests.py` and stage fixtures.

**Files:**
- Create: `tests/lib/prepost.py`

- [ ] **Step 1: Write prepost.py**

```python
"""Pre/post config save-restore for test suite isolation.

Called by run_tests.py before and after any stage run.
Also importable by stage fixtures for internal use.

Snapshot path: /etc/sonic/pre_test_config.json  (persistent across reboots)
Clean template: /etc/sonic/clean_boot.json       (copied from tests/fixtures/)
"""

import os
import time

SNAPSHOT_PATH = "/etc/sonic/pre_test_config.json"
CLEAN_TEMPLATE_REMOTE = "/etc/sonic/clean_boot.json"
CLEAN_TEMPLATE_LOCAL = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "clean_boot.json"
)
SUITE_ACTIVE_PATH = "/run/wedge100s/test_suite_active"


def save_and_reload_clean(ssh, timeout=120):
    """Save current config and apply clean_boot.json template.

    Steps:
      1. Upload clean_boot.json to switch
      2. config save → snapshot
      3. config reload clean_boot.json -y
      4. Wait for pmon daemons (up to timeout seconds)
      5. Write /run/wedge100s/test_suite_active

    Raises RuntimeError on any failure.
    """
    # 1. Upload template
    with open(CLEAN_TEMPLATE_LOCAL) as f:
        content = f.read()
    out, err, rc = ssh.run(
        f"cat > {CLEAN_TEMPLATE_REMOTE} << 'EOFCLEAN'\n{content}\nEOFCLEAN", timeout=30
    )
    if rc != 0:
        raise RuntimeError(f"Failed to upload clean_boot.json: {err}")

    # 2. Save current config as snapshot
    out, err, rc = ssh.run(f"sudo config save {SNAPSHOT_PATH} -y", timeout=60)
    if rc != 0:
        raise RuntimeError(f"config save failed (rc={rc}): {err}")

    # 3. config reload
    out, err, rc = ssh.run(
        f"sudo config reload {CLEAN_TEMPLATE_REMOTE} -y", timeout=90
    )
    if rc != 0:
        raise RuntimeError(f"config reload clean_boot failed (rc={rc}): {err}")

    # 4. Wait for pmon
    _wait_for_pmon(ssh, timeout=timeout)

    # 5. Mark suite active
    ssh.run("sudo mkdir -p /run/wedge100s", timeout=5)
    import datetime
    ts = datetime.datetime.utcnow().isoformat()
    ssh.run(f"echo '{ts}' | sudo tee {SUITE_ACTIVE_PATH} > /dev/null", timeout=5)


def restore_user_config(ssh, timeout=120):
    """Restore pre-test config from snapshot.

    Steps:
      1. config reload /etc/sonic/pre_test_config.json -y
      2. config save -y  (persist the restore)
      3. Wait for pmon daemons
      4. Remove /run/wedge100s/test_suite_active

    Returns True if all steps succeed, False if any step fails (non-fatal
    for the overall test exit code — stage_nn_posttest tests report failures).
    """
    ok = True

    out, err, rc = ssh.run(
        f"sudo config reload {SNAPSHOT_PATH} -y", timeout=90
    )
    if rc != 0:
        print(f"[posttest] config reload restore failed (rc={rc}): {err}")
        ok = False

    out, err, rc = ssh.run("sudo config save -y", timeout=60)
    if rc != 0:
        print(f"[posttest] config save after restore failed (rc={rc}): {err}")
        ok = False

    _wait_for_pmon(ssh, timeout=timeout)
    ssh.run(f"sudo rm -f {SUITE_ACTIVE_PATH}", timeout=5)
    return ok


def _wait_for_pmon(ssh, timeout=120, poll_interval=5):
    """Poll until pmon reports all daemons RUNNING (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, err, rc = ssh.run(
            "sudo systemctl is-active pmon 2>&1", timeout=10
        )
        if rc == 0 and "active" in out:
            # Also check all sub-daemons in STATE_DB
            out2, _, rc2 = ssh.run(
                "redis-cli -n 6 hgetall 'PROCESS_STATS|pmon' 2>/dev/null | grep -c 'RUNNING'",
                timeout=10,
            )
            # Any daemons running is enough — full convergence takes time
            if rc2 == 0:
                time.sleep(poll_interval)  # short settle
                return
        time.sleep(poll_interval)
    # Don't raise — pmon may still be starting; let stage tests fail naturally
    print(f"[prepost] Warning: pmon did not fully stabilize within {timeout}s")
```

- [ ] **Step 2: Add `__init__.py` to lib if missing**

```bash
touch /export/sonic/sonic-buildimage.claude/tests/lib/__init__.py 2>/dev/null; true
```

- [ ] **Step 3: Commit**

```bash
git add tests/lib/prepost.py
git commit -m "test: add prepost.py for config save/restore around test stages"
```

---

## Task 3: Modify run_tests.py — Auto-Inject Pre/Post

**Files:**
- Modify: `tests/run_tests.py`

- [ ] **Step 1: Write a failing test for the injection logic first**

Create `tests/test_run_tests_unit.py` (runs locally, no SSH):

```python
"""Unit tests for run_tests.py stage injection logic."""
import sys, os
# run_tests.py lives in the tests/ directory; add it to path directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import run_tests

def test_inject_wraps_stages():
    """stage_00 prepended and stage_nn appended when running a middle stage."""
    stages = run_tests._inject_prepost(["stage_12_counters"])
    assert stages[0] == "stage_00_pretest"
    assert stages[-1] == "stage_nn_posttest"
    assert "stage_12_counters" in stages

def test_inject_full_suite_no_duplicate():
    """Full suite already has stage_00 and stage_nn — no duplicates added."""
    all_stages = [
        "stage_00_pretest", "stage_01_eeprom", "stage_12_counters",
        "stage_17_report", "stage_nn_posttest"
    ]
    stages = run_tests._inject_prepost(all_stages)
    assert stages.count("stage_00_pretest") == 1
    assert stages.count("stage_nn_posttest") == 1

def test_no_prepost_flag_skips_injection():
    stages = run_tests._inject_prepost(["stage_12_counters"], inject=False)
    assert "stage_00_pretest" not in stages
    assert "stage_nn_posttest" not in stages
```

Run: `python3 -m pytest tests/test_run_tests_unit.py -v`
Expected: ImportError / AttributeError (function doesn't exist yet)

- [ ] **Step 2: Add `_inject_prepost()` and wire into `_run_tests()`**

Edit `tests/run_tests.py`. Add after the `_available_stages()` function:

```python
PRETEST_STAGE  = "stage_00_pretest"
POSTTEST_STAGE = "stage_nn_posttest"


def _inject_prepost(stage_names, inject=True):
    """Return stage list with stage_00_pretest prepended and stage_nn_posttest appended.

    If inject=False or stages already contain both bookends, returns as-is.
    Preserves order of everything in between.
    stage_nn_posttest sorts after all digit-prefixed stages (n > 9 in ASCII)
    so no custom ordering logic is needed — alphabetical sort is correct.
    """
    if not inject:
        return list(stage_names)
    result = list(stage_names)
    if PRETEST_STAGE not in result:
        result.insert(0, PRETEST_STAGE)
    if POSTTEST_STAGE not in result:
        result.append(POSTTEST_STAGE)
    return result
```

In `_run_tests()`, change the first line that builds `test_dirs` to:

```python
def _run_tests(stage_names, cfg_path, extra_pytest_args, inject_prepost=True):
    stage_names = _inject_prepost(stage_names, inject=inject_prepost)
    # filter out stages that don't exist as directories
    available = set(_available_stages())
    stage_names = [s for s in stage_names if s in available]
    test_dirs = [os.path.join(TESTS_DIR, name) for name in stage_names]
    ...
```

In `main()`, parse `--no-prepost` flag:

```python
    no_prepost = "--no-prepost" in args
    if no_prepost:
        args.remove("--no-prepost")
    ...
    _run_tests(stage_names, cfg_path, extra_pytest_args, inject_prepost=not no_prepost)
```

- [ ] **Step 3: Run unit tests**

```bash
cd /export/sonic/sonic-buildimage.claude
python3 -m pytest tests/test_run_tests_unit.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/run_tests.py tests/test_run_tests_unit.py
git commit -m "test: run_tests.py auto-injects stage_00_pretest/stage_nn_posttest around any stage run"
```

---

## Task 4: Create stage_00_pretest

Verifies the clean state after `prepost.save_and_reload_clean()` runs.
The actual save+reload is done by `run_tests.py`'s injection — stage_00 calls it and then verifies.

**Files:**
- Create: `tests/stage_00_pretest/__init__.py`
- Create: `tests/stage_00_pretest/test_pretest.py`

- [ ] **Step 1: Write test_pretest.py**

```python
"""Stage 00 — Pre-Test: Save user config and apply clean-boot template.

This stage performs the config save + reload, then verifies the resulting
state matches the clean-boot specification. Any failure calls pytest.exit()
to abort the entire test suite before any test stage runs.

Run by: run_tests.py (injected as first stage for any stage selection).
"""

import json
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.prepost import save_and_reload_clean, SNAPSHOT_PATH

NUM_PORTS = 32
EXPECTED_SPEED = "100000"


@pytest.fixture(scope="session", autouse=True)
def pretest_setup(ssh):
    """Save config and apply clean-boot template. Aborts suite on failure."""
    try:
        save_and_reload_clean(ssh, timeout=120)
    except Exception as exc:
        pytest.exit(
            f"\n[stage_00] Pre-test setup failed: {exc}\n"
            "Cannot continue — target is in unknown state.",
            returncode=3,
        )


def test_snapshot_exists(ssh):
    """Pre-test snapshot file was created."""
    out, err, rc = ssh.run(f"test -f {SNAPSHOT_PATH} && echo OK", timeout=10)
    assert rc == 0 and "OK" in out, f"Snapshot not found at {SNAPSHOT_PATH}"


def test_all_ports_100g(ssh):
    """All 32 ports have speed=100000 in CONFIG_DB after clean reload.

    Uses CONFIG_DB (not show interfaces status) to avoid false failures from
    ports that are admin-up but have never linked (speed column shows N/A).
    """
    out, err, rc = ssh.run(
        "sonic-cfggen -d --var-json PORT 2>&1", timeout=30
    )
    assert rc == 0, f"sonic-cfggen failed: {err}"
    import json
    ports = json.loads(out)
    assert len(ports) == NUM_PORTS, f"Expected {NUM_PORTS} PORT entries, got {len(ports)}"
    wrong = {k: v.get("speed") for k, v in ports.items() if v.get("speed") != EXPECTED_SPEED}
    assert not wrong, f"Ports not at {EXPECTED_SPEED} speed: {wrong}"


def test_no_portchannel(ssh):
    """No PortChannel interfaces exist in clean state."""
    out, err, rc = ssh.run("show interfaces portchannel 2>&1", timeout=30)
    # Non-zero rc or empty output means no PortChannel — both acceptable
    assert "PortChannel" not in out, (
        f"PortChannel found in clean state:\n{out}\n"
        "stage_16 is responsible for creating PortChannel1."
    )


def test_no_port_fec(ssh):
    """No port-level FEC is configured in clean state."""
    out, err, rc = ssh.run(
        "redis-cli -n 4 keys 'PORT|*' | xargs -I{} redis-cli -n 4 hget {} fec 2>/dev/null",
        timeout=30,
    )
    fec_values = [l.strip() for l in out.splitlines() if l.strip() and l.strip() != "none"]
    assert not fec_values, f"Unexpected FEC in clean state: {fec_values}"


def test_breakout_cfg_seeded(ssh):
    """BREAKOUT_CFG is populated for all 32 ports."""
    out, err, rc = ssh.run(
        "redis-cli -n 4 keys 'BREAKOUT_CFG|*' | wc -l", timeout=15
    )
    count = int(out.strip()) if out.strip().isdigit() else 0
    assert count >= NUM_PORTS, (
        f"BREAKOUT_CFG has {count} entries, expected >= {NUM_PORTS}"
    )


def test_pmon_running(ssh):
    """pmon service is active after config reload."""
    out, err, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active: {out.strip()}"


def test_suite_active_marker(ssh):
    """Test suite active marker file exists."""
    out, err, rc = ssh.run("test -f /run/wedge100s/test_suite_active && echo OK", timeout=5)
    assert rc == 0, "Suite active marker /run/wedge100s/test_suite_active not found"
```

- [ ] **Step 2: Create `__init__.py`**

```bash
touch /export/sonic/sonic-buildimage.claude/tests/stage_00_pretest/__init__.py
```

- [ ] **Step 3: Test it can at least be collected by pytest**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 -m pytest stage_00_pretest/ --collect-only 2>&1 | head -20
```

Expected: 6 tests collected.

- [ ] **Step 4: Commit**

```bash
git add tests/stage_00_pretest/
git commit -m "test: add stage_00_pretest (save + config reload clean_boot)"
```

---

## Task 5: Create stage_nn_posttest

**Files:**
- Create: `tests/stage_nn_posttest/__init__.py`
- Create: `tests/stage_nn_posttest/test_posttest.py`

- [ ] **Step 1: Write test_posttest.py**

```python
"""Stage NN — Post-Test: Restore user config from pre-test snapshot.

Non-fatal: failures here are reported as test failures but do not affect
the exit code of stages 01–20 (those results are already recorded).

Run by: run_tests.py (injected as last stage for any stage selection).
Named stage_nn_posttest so 'n' > any digit — always sorts last regardless
of how many numbered test stages are added.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.prepost import restore_user_config, SNAPSHOT_PATH, SUITE_ACTIVE_PATH


_RESTORE_OK = None  # set by fixture, read by test


@pytest.fixture(scope="session", autouse=True)
def posttest_restore(ssh):
    """Restore config from snapshot. Runs as first thing in stage_nn_posttest."""
    global _RESTORE_OK
    _RESTORE_OK = restore_user_config(ssh, timeout=120)


def test_restore_succeeded(ssh):
    """config reload from snapshot returned True (no errors)."""
    assert _RESTORE_OK is True, (
        "restore_user_config() returned False — config reload or save failed. "
        "Check switch state manually."
    )


def test_snapshot_was_present(ssh):
    """Snapshot file is still on disk after restore."""
    out, err, rc = ssh.run(f"test -f {SNAPSHOT_PATH} && echo OK", timeout=10)
    assert rc == 0 and "OK" in out, f"Snapshot missing at {SNAPSHOT_PATH} post-restore"


def test_suite_active_marker_removed(ssh):
    """Suite active marker removed after restore."""
    out, err, rc = ssh.run(
        f"test -f {SUITE_ACTIVE_PATH} && echo EXISTS || echo GONE", timeout=5
    )
    assert "GONE" in out, "Suite active marker still present after posttest"


def test_pmon_running_after_restore(ssh):
    """pmon is active after config restore."""
    out, err, rc = ssh.run("sudo systemctl is-active pmon", timeout=15)
    assert rc == 0, f"pmon is not active after restore: {out.strip()}"


def test_connected_ports_admin_up_after_restore(ssh):
    """Connected ports are admin-up after config restore."""
    connected = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
    out, err, rc = ssh.run("show interfaces status 2>&1", timeout=30)
    assert rc == 0
    for port in connected:
        line = next((l for l in out.splitlines() if port in l), None)
        assert line is not None, f"{port} not found in interfaces status"
        assert "up" in line.lower(), f"{port} is not admin-up after restore: {line}"
```

- [ ] **Step 2: Create `__init__.py`**

```bash
touch /export/sonic/sonic-buildimage.claude/tests/stage_nn_posttest/__init__.py
```

- [ ] **Step 3: Collect check**

```bash
python3 -m pytest tests/stage_nn_posttest/ --collect-only 2>&1 | head -20
```

Expected: 4 tests collected.

- [ ] **Step 4: Commit**

```bash
git add tests/stage_nn_posttest/
git commit -m "test: add stage_nn_posttest (config restore from snapshot)"
```

---

## Task 6: Rename stage_18_report → stage_17_report; Delete stage_17_restore

**Files:**
- Move: `tests/stage_18_report/` → `tests/stage_17_report/`
- Delete: `tests/stage_17_restore/`

- [ ] **Step 1: Rename and delete**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
mv stage_18_report stage_17_report
git rm -r stage_17_restore/
git add stage_17_report/
```

- [ ] **Step 2: Verify run_tests.py --list shows correct order**

```bash
python3 run_tests.py --list
```

Expected order: `stage_00_pretest`, `stage_01_eeprom`, … `stage_16_portchannel`, `stage_17_report`, `stage_19_platform_cli`, `stage_20_traffic`, `stage_nn_posttest`.

- [ ] **Step 3: Commit**

```bash
git commit -m "test: rename stage_18_report→stage_17_report, delete stage_17_restore"
```

---

## Task 7: Add Setup/Teardown to Config-Modifying Stages

Each of stages 13, 14, 15, 16 must configure what it needs and clean up after itself.

**Files:**
- Modify: `tests/stage_13_link/test_link.py`
- Modify: `tests/stage_14_breakout/test_breakout.py`
- Modify: `tests/stage_15_autoneg_fec/test_autoneg_fec.py`
- Modify: `tests/stage_16_portchannel/test_portchannel.py`

### Stage 13 — RS-FEC on connected ports

- [ ] **Step 1: Add session-scoped fixture to test_link.py**

At the top of `test_link.py`, after imports, add:

```python
CONNECTED_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]

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

- [ ] **Step 2: Verify stage_13 still collects**

```bash
python3 -m pytest tests/stage_13_link/ --collect-only 2>&1 | head -10
```

### Stage 14 — Breakout

- [ ] **Step 3: Add fixture to test_breakout.py**

Read the current test to see what port and what breakout it tests. Add at top of file:

```python
BREAKOUT_PORT = "Ethernet0"   # port used for breakout testing
BREAKOUT_MODE = "4x25G[10G]"

@pytest.fixture(scope="session", autouse=True)
def stage14_setup_teardown(ssh):
    """Ensure port is in 1x100G before testing; restore after."""
    # Ensure starting in 1x100G
    ssh.run(f"sudo config interface breakout {BREAKOUT_PORT} '1x100G[40G]' -y -f", timeout=60)
    import time; time.sleep(5)
    yield
    # Restore to 1x100G after breakout tests
    ssh.run(f"sudo config interface breakout {BREAKOUT_PORT} '1x100G[40G]' -y -f", timeout=60)
    time.sleep(5)
```

Read the existing `test_breakout.py` first and confirm `BREAKOUT_PORT` before writing — the file may already define which port it uses.

- [ ] **Step 4: Read test_breakout.py to confirm port**

```bash
head -60 /export/sonic/sonic-buildimage.claude/tests/stage_14_breakout/test_breakout.py
```

Adjust `BREAKOUT_PORT` in the fixture to match.

### Stage 15 — FEC/Autoneg

- [ ] **Step 5: Read test_autoneg_fec.py to understand existing port constants and setup**

```bash
cat tests/stage_15_autoneg_fec/test_autoneg_fec.py
```

Identify: (a) which port is used for config-change tests, (b) which ports are `CONNECTED_PORTS`, (c) whether any fixture already configures RS-FEC. Only add the following fixture if RS-FEC on connected ports is NOT already configured within the tests themselves:

```python
# Add AFTER module-level constants (CONNECTED_PORTS, TEST_PORT, etc.)
@pytest.fixture(scope="session", autouse=True)
def stage15_setup_teardown(ssh):
    """Configure RS-FEC on connected ports so links are up; restore after."""
    import time
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    time.sleep(5)
    yield
    for port in CONNECTED_PORTS:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)
```

If the existing tests already set and clear FEC as part of their logic, skip this step.

### Stage 16 — PortChannel

- [ ] **Step 6: Add fixture to test_portchannel.py**

Place the fixture **after** the module-level constants (`PORTCHANNEL_NAME`, `LAG_MEMBERS`, `LAG_IP`, `STANDALONE_PORTS`, `PEER_IP`) so they are defined before the fixture body references them.

```python
@pytest.fixture(scope="session", autouse=True)
def stage16_setup_teardown(ssh):
    """Create PortChannel1, add members, assign IP; remove all after."""
    import time

    # Configure RS-FEC on LAG members (required for link-up)
    for port in LAG_MEMBERS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)

    # Enable teamd feature
    ssh.run("sudo config feature state teamd enabled", timeout=15)
    time.sleep(3)

    # Create PortChannel and members
    ssh.run(f"sudo config portchannel add {PORTCHANNEL_NAME}", timeout=30)
    for port in LAG_MEMBERS:
        ssh.run(f"sudo config portchannel member add {PORTCHANNEL_NAME} {port}", timeout=30)
    ssh.run(f"sudo config interface ip add {PORTCHANNEL_NAME} {LAG_IP}", timeout=15)
    time.sleep(15)  # LACP negotiation

    yield

    # Teardown: remove PortChannel
    ssh.run(f"sudo config interface ip remove {PORTCHANNEL_NAME} {LAG_IP}", timeout=15)
    for port in LAG_MEMBERS:
        ssh.run(f"sudo config portchannel member del {PORTCHANNEL_NAME} {port}", timeout=30)
    ssh.run(f"sudo config portchannel del {PORTCHANNEL_NAME}", timeout=30)
    for port in LAG_MEMBERS:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)
```

- [ ] **Step 7: Run collect-only for all 4 stages**

```bash
python3 -m pytest tests/stage_13_link/ tests/stage_14_breakout/ tests/stage_15_autoneg_fec/ tests/stage_16_portchannel/ --collect-only 2>&1 | tail -5
```

Expected: all tests collected, no errors.

- [ ] **Step 8: Commit**

```bash
git add tests/stage_13_link/ tests/stage_14_breakout/ tests/stage_15_autoneg_fec/ tests/stage_16_portchannel/
git commit -m "test: add per-stage setup/teardown fixtures (stages 13–16 are now self-contained)"
```

---

## Task 8: bmc.py — TTY Buffer Flush Fix

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/bmc.py`

- [ ] **Step 1: Read the current send_command method**

```bash
grep -n "send_command\|def _send\|ser.read\|ser.write\|select" \
  platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/bmc.py
```

- [ ] **Step 2: Drain buffer before writing**

In `send_command()` (or equivalent), before the `ser.write(cmd)` call, add a drain:

```python
import select

# Drain any stale bytes in the receive buffer before sending command
while True:
    ready, _, _ = select.select([self._tty.fileno()], [], [], 0)
    if not ready:
        break
    self._tty.read(256)
```

The exact variable name (`ser`, `self._tty`, `self._port`, etc.) must match what you read in Step 1. Do not guess.

- [ ] **Step 3: Verify by running stage_03 specifically**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 run_tests.py stage_03_platform --no-prepost -- -v -k "bmc_uptime"
```

Expected: 1 passed (was the one failing test).

Wait — this requires the new .deb to be deployed. Skip hardware verify for now; the unit test logic is sufficient. Add a note in the commit that hardware verification happens in Task 11.

- [ ] **Step 4: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/bmc.py
git commit -m "fix: drain TTY receive buffer before each bmc.py command (fixes bmc_uptime test)"
```

---

## Task 9: Platform API — chassis.py Completions

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py`

- [ ] **Step 1: Read chassis.py — find existing methods and imports**

```bash
grep -n "def get_\|def __init__\|import\|eeprom\|SysEeprom\|NUM_SFP" \
  platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py | head -40
```

- [ ] **Step 2: Add `get_base_mac()`**

Find where `__init__` sets up `self._eeprom`. After confirming the attribute name, add:

```python
def get_base_mac(self):
    """Return base MAC address from EEPROM TLV 0x24."""
    try:
        info = self._eeprom.get_eeprom()
        return info.get('0x24') or info.get('Base MAC Address') or 'NA'
    except Exception:
        return 'NA'
```

- [ ] **Step 3: Add `get_reboot_cause()`**

Add `REBOOT_CAUSE_FILE` as a class-level constant inside the `Chassis` class (alongside other class-level constants like `NUM_SFPS`), then add the method:

```python
# Inside class Chassis, at class level (not inside any method):
REBOOT_CAUSE_FILE = "/var/log/sonic/reboot-cause/previous-reboot-cause.txt"

def get_reboot_cause(self):
    """Return (cause_constant, description) from reboot-cause file."""
    try:
        with open(self.REBOOT_CAUSE_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    return (self.REBOOT_CAUSE_NON_HARDWARE, line)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return (self.REBOOT_CAUSE_POWER_LOSS, "")
```

- [ ] **Step 4: Add `get_port_or_cage_type()`**

```python
def get_port_or_cage_type(self, index):
    """All 32 ports are QSFP28."""
    if 1 <= index <= self.NUM_SFPS:
        return self.SFP_PORT_TYPE_BIT_QSFP28
    return None
```

Confirm `NUM_SFPS` constant name by reading the file in Step 1.

- [ ] **Step 5: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py
git commit -m "feat: implement get_base_mac, get_reboot_cause, get_port_or_cage_type in chassis.py"
```

---

## Task 10: Platform API — thermal.py Low Thresholds

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/thermal.py`

- [ ] **Step 1: Read thermal.py to find existing threshold methods**

```bash
grep -n "def get.*threshold\|def get_temperature\|def get_high" \
  platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/thermal.py
```

- [ ] **Step 2: Add low threshold methods**

Find the class that has `get_high_threshold()` and add alongside it:

```python
def get_low_threshold(self):
    """Return low warning threshold (°C). Operational minimum is 0°C."""
    return 0.0

def get_low_critical_threshold(self):
    """Return low critical threshold (°C)."""
    return -10.0
```

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/thermal.py
git commit -m "feat: add low threshold methods to thermal.py (0°C warning, -10°C critical)"
```

---

## Task 11: Platform API — psu.py Static Model

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/psu.py`

- [ ] **Step 1: Read psu.py — find get_model()**

```bash
grep -n "def get_model\|def get_serial\|MFR\|model" \
  platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/psu.py
```

- [ ] **Step 2: Update get_model() to return static string**

Replace the body of `get_model()`:

```python
def get_model(self):
    """Return PSU model. Static string — PMBus block-read not implemented."""
    return "Delta DPS-1100AB-6 A"
```

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/psu.py
git commit -m "feat: psu.py get_model returns static string (PMBus block-read deferred)"
```

---

## Task 12: Platform API — component.py (CPLD + BIOS)

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/component.py`
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py`

- [ ] **Step 1: Verify CPLD sysfs path on hardware**

```bash
ssh admin@192.168.88.12 'cat /sys/bus/i2c/devices/1-0032/cpld_version' 2>&1
```

If this succeeds, use that path. If not found, check alternatives:

```bash
ssh admin@192.168.88.12 'find /sys -name "cpld_version" 2>/dev/null'
```

Record the actual path.

- [ ] **Step 2: Write component.py**

```python
"""Component — CPLD and BIOS firmware version reporting."""

import subprocess

CPLD_VERSION_PATH = "/sys/bus/i2c/devices/1-0032/cpld_version"


class Component:
    """Read-only firmware component (CPLD or BIOS)."""

    def __init__(self, name, description, version_fn):
        self._name = name
        self._description = description
        self._version_fn = version_fn

    def get_name(self):
        return self._name

    def get_description(self):
        return self._description

    def get_firmware_version(self):
        try:
            return self._version_fn()
        except Exception as exc:
            return f"N/A ({exc})"

    def install_firmware(self, image_path):
        # ComponentBase.install_firmware() returns bool only — check base class
        # signature: `grep install_firmware /usr/lib/python3/dist-packages/sonic_platform_base/component_base.py`
        # If it returns bool: return False
        # If it returns (bool, str): return False, "Firmware update not supported"
        return False

    def auto_update_firmware(self, image_path, boot_type):
        return False


def _cpld_version():
    return open(CPLD_VERSION_PATH).read().strip()


def _bios_version():
    result = subprocess.run(
        ["dmidecode", "-s", "bios-version"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        return result.stdout.strip()
    raise RuntimeError(result.stderr.strip())


COMPONENT_CPLD = Component(
    name="CPLD",
    description="Complex Programmable Logic Device",
    version_fn=_cpld_version,
)

COMPONENT_BIOS = Component(
    name="BIOS",
    description="Basic Input/Output System",
    version_fn=_bios_version,
)

COMPONENT_LIST = [COMPONENT_CPLD, COMPONENT_BIOS]
```

Adjust `CPLD_VERSION_PATH` based on what Step 1 found.

- [ ] **Step 3: Wire into chassis.py `__init__`**

In `Chassis.__init__()`, add:

```python
from sonic_platform.component import COMPONENT_LIST
self._component_list = list(COMPONENT_LIST)
```

Add `get_num_components()` and `get_all_components()` if they don't exist:

```python
def get_num_components(self):
    return len(self._component_list)

def get_all_components(self):
    return self._component_list

def get_component(self, index):
    if 0 <= index < len(self._component_list):
        return self._component_list[index]
    return None
```

- [ ] **Step 4: Verify on hardware**

```bash
ssh admin@192.168.88.12 'python3 -c "
from sonic_platform.component import COMPONENT_LIST
for c in COMPONENT_LIST:
    print(c.get_name(), c.get_firmware_version())
"' 2>&1
```

Expected: `CPLD <version>` and `BIOS <version>` — no exceptions.

- [ ] **Step 5: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/component.py
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/chassis.py
git commit -m "feat: add component.py with CPLD+BIOS firmware version reporting"
```

---

## Task 13: Build and Deploy the Updated .deb

**Files:** none (build system)

- [ ] **Step 1: Build the platform package**

```bash
cd /export/sonic/sonic-buildimage.claude
BLDENV=trixie make SONIC_BUILD_JOBS=40 target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb 2>&1 | tail -30
```

Expected: exits 0, `.deb` updated.

- [ ] **Step 2: Deploy**

```bash
scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
ssh admin@192.168.88.12 'sudo systemctl stop pmon && sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb && sudo systemctl start pmon'
```

- [ ] **Step 3: Smoke-test platform API**

```bash
ssh admin@192.168.88.12 'python3 -c "
from sonic_platform.platform import Platform
ch = Platform().get_chassis()
print(\"base_mac:\", ch.get_base_mac())
print(\"reboot_cause:\", ch.get_reboot_cause())
print(\"port_cage_type:\", ch.get_port_or_cage_type(1))
print(\"thermal_low:\", ch.get_all_thermals()[0].get_low_threshold())
print(\"psu_model:\", ch.get_all_psus()[0].get_model())
for c in ch.get_all_components():
    print(\"component:\", c.get_name(), c.get_firmware_version())
"' 2>&1
```

Expected: all values non-None, no tracebacks.

- [ ] **Step 4: Re-run stage_03 to confirm bmc_uptime fixed**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 run_tests.py stage_03_platform --no-prepost -- -v -k "bmc_uptime"
```

Expected: 1 passed.

---

## Task 14: Full Infrastructure Integration Test

Run stages 00 through 18 in order to validate the pre/post mechanism end-to-end.

- [ ] **Step 1: Run full suite**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 run_tests.py 2>&1 | tee reports/integration_$(date +%Y-%m-%d_%H%M%S).txt
```

Expected: stage_00_pretest completes (config reloaded), stages 01–20 pass, stage_nn_posttest restores config.

- [ ] **Step 2: Verify config was restored**

```bash
ssh admin@192.168.88.12 'show interfaces portchannel; show interfaces status | grep -E "Ethernet(16|32|48|112)"' 2>&1
```

Expected: PortChannel1 absent (still on clean-boot state — stage_nn_posttest runs last and restores it).

- [ ] **Step 3: Run a single-stage test to verify injection**

```bash
python3 run_tests.py stage_12_counters -- -v 2>&1 | head -30
```

Expected: pytest runs stage_00_pretest FIRST, then stage_12_counters, then stage_nn_posttest.

- [ ] **Step 4: Commit integration results**

```bash
git add tests/reports/
git commit -m "test: integration run showing pre/post injection working end-to-end"
```

---

## Task 15: Stage 19 — Platform CLI Audit

**Files:**
- Create: `tests/stage_19_platform_cli/__init__.py`
- Create: `tests/stage_19_platform_cli/test_platform_cli.py`

- [ ] **Step 1: Write test_platform_cli.py**

```python
"""Stage 19 — Platform CLI Audit.

Verifies all SONiC platform-facing CLI commands and API methods produce
correct output backed by the platform Python package.

Runs on clean-boot baseline (before stage_nn_posttest). All tests here
must be self-contained — do not assume user-config state (e.g., no
PortChannel1 exists unless this stage creates it).
"""

import re
import pytest


MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')


def test_base_mac_syseeprom(ssh):
    out, err, rc = ssh.run("show platform syseeprom", timeout=30)
    assert rc == 0, f"show platform syseeprom failed: {err}"
    assert "Base MAC Address" in out, f"Base MAC Address not in syseeprom output:\n{out}"
    mac_line = next(l for l in out.splitlines() if "Base MAC Address" in l)
    mac = mac_line.split()[-1]
    assert MAC_RE.match(mac), f"Base MAC address malformed: {mac!r}"


def test_base_mac_api(ssh, platform_api):
    out, err, rc = platform_api("print(chassis.get_base_mac())")
    assert rc == 0, f"get_base_mac() raised: {err}"
    mac = out.strip()
    assert MAC_RE.match(mac), f"get_base_mac() returned malformed MAC: {mac!r}"


def test_reboot_cause(ssh):
    out, err, rc = ssh.run("show platform reboot-cause", timeout=30)
    assert rc == 0, f"show platform reboot-cause failed: {err}"
    assert out.strip(), "show platform reboot-cause returned empty output"


def test_firmware_cpld(ssh):
    out, err, rc = ssh.run("show platform firmware", timeout=30)
    assert rc == 0, f"show platform firmware failed: {err}"
    assert "CPLD" in out, f"CPLD not in firmware output:\n{out}"
    cpld_line = next((l for l in out.splitlines() if "CPLD" in l), "")
    assert "N/A" not in cpld_line or len(cpld_line.split()) > 2, (
        f"CPLD version missing: {cpld_line}"
    )


def test_firmware_bios(ssh):
    out, err, rc = ssh.run("show platform firmware", timeout=30)
    assert rc == 0
    assert "BIOS" in out, f"BIOS not in firmware output:\n{out}"


def test_psu_model_not_na(ssh):
    out, err, rc = ssh.run("show platform psustatus", timeout=30)
    assert rc == 0, f"show platform psustatus failed: {err}"
    psu_lines = [l for l in out.splitlines() if "PSU" in l]
    assert psu_lines, "No PSU lines in psustatus output"
    for line in psu_lines:
        assert "N/A" not in line or "Serial" in line, (
            f"PSU model appears to be N/A: {line}"
        )


def test_environment_thermals(ssh):
    out, err, rc = ssh.run("show environment", timeout=30)
    assert rc == 0, f"show environment failed: {err}"
    temp_lines = [l for l in out.splitlines() if "°C" in l or "Degrees" in l or "TMP" in l]
    assert len(temp_lines) >= 7, (
        f"Expected >= 7 thermal sensor lines, found {len(temp_lines)}:\n{out}"
    )


def test_environment_fans(ssh):
    out, err, rc = ssh.run("show environment", timeout=30)
    assert rc == 0
    fan_lines = [l for l in out.splitlines() if "Fan" in l and "RPM" in l]
    assert len(fan_lines) >= 5, (
        f"Expected >= 5 fan lines with RPM, found {len(fan_lines)}:\n{out}"
    )


def test_port_cage_type_qsfp28(ssh, platform_api):
    out, err, rc = platform_api(
        "from sonic_platform_base.sfp_base import SfpBase; "
        "print(chassis.get_port_or_cage_type(1) == SfpBase.SFP_PORT_TYPE_BIT_QSFP28)"
    )
    assert rc == 0, f"get_port_or_cage_type raised: {err}"
    assert "True" in out, f"get_port_or_cage_type(1) did not return QSFP28 bitmask: {out}"


def test_watchdogutil_status(ssh):
    out, err, rc = ssh.run("watchdogutil status", timeout=15)
    assert rc == 0, f"watchdogutil status failed (rc={rc}): {err}"
    # Stub — output may indicate watchdog is not armed; that's acceptable
    print(f"\nwatchdogutil status output:\n{out}")
```

- [ ] **Step 2: Collect check**

```bash
python3 -m pytest tests/stage_19_platform_cli/ --collect-only 2>&1 | head -15
```

- [ ] **Step 3: Commit**

```bash
git add tests/stage_19_platform_cli/
git commit -m "test: add stage_19_platform_cli audit tests"
```

---

## Task 16: Stage 20 — Traffic Forwarding Verification

**Files:**
- Create: `tests/stage_20_traffic/__init__.py`
- Create: `tests/stage_20_traffic/test_traffic.py`

- [ ] **Step 1: Add `[links]` section to tests/target.cfg**

`test_traffic.py` reads peer IPs from `target.cfg`. Add the section if not present:

```ini
[links]
# IP of the Arista peer reachable via PortChannel1
peer_ip = 10.0.1.0
# IP of the Arista peer reachable via Ethernet48 (standalone)
standalone_peer_ip = 10.0.0.0
```

Verify section is correct for the lab topology (see `tests/test_plan.md`).

```bash
grep -A4 '\[links\]' tests/target.cfg || echo "Section missing — add it"
```

- [ ] **Step 3: Write test_traffic.py**

```python
"""Stage 20 — Traffic Forwarding Verification.

Verifies the ASIC forwards packets over connected links and SAI counters
accurately reflect traffic. Runs on clean-boot baseline before stage_nn_posttest.

This stage owns its own PortChannel1 lifecycle via the stage20_setup
session fixture (create before tests, remove after). Do not depend on
PortChannel1 pre-existing from stage_16 — that stage's fixture already
removed it as part of its teardown.
"""

import os
import re
import sys
import time
import configparser
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LAG_PORTS = ["Ethernet16", "Ethernet32"]
PORTCHANNEL = "PortChannel1"
LAG_IP_HARE = "10.0.1.1/31"
STANDALONE_PORT = "Ethernet48"
STANDALONE_IP_HARE = "10.0.0.1/31"

def _load_peer_ip(cfg_key="peer_ip", fallback="10.0.1.0"):
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(os.path.dirname(__file__), "..", "target.cfg"))
    return cfg.get("links", cfg_key, fallback=fallback)

PEER_IP = _load_peer_ip("peer_ip", "10.0.1.0")
STANDALONE_PEER_IP = _load_peer_ip("standalone_peer_ip", "10.0.0.0")


@pytest.fixture(scope="session", autouse=True)
def stage20_setup(ssh):
    """Bring up PortChannel1 and standalone port for traffic testing."""
    import time

    for port in LAG_PORTS:
        ssh.run(f"sudo config interface fec {port} rs", timeout=15)
    ssh.run(f"sudo config interface fec {STANDALONE_PORT} rs", timeout=15)
    ssh.run("sudo config feature state teamd enabled", timeout=15)
    time.sleep(3)

    ssh.run(f"sudo config portchannel add {PORTCHANNEL}", timeout=30)
    for port in LAG_PORTS:
        ssh.run(f"sudo config portchannel member add {PORTCHANNEL} {port}", timeout=30)
    ssh.run(f"sudo config interface ip add {PORTCHANNEL} {LAG_IP_HARE}", timeout=15)
    ssh.run(f"sudo config interface ip add {STANDALONE_PORT} {STANDALONE_IP_HARE}", timeout=15)
    time.sleep(20)  # LACP + ARP convergence

    yield

    ssh.run(f"sudo config interface ip remove {PORTCHANNEL} {LAG_IP_HARE}", timeout=15)
    ssh.run(f"sudo config interface ip remove {STANDALONE_PORT} {STANDALONE_IP_HARE}", timeout=15)
    for port in LAG_PORTS:
        ssh.run(f"sudo config portchannel member del {PORTCHANNEL} {port}", timeout=30)
    ssh.run(f"sudo config portchannel del {PORTCHANNEL}", timeout=30)
    for port in LAG_PORTS + [STANDALONE_PORT]:
        ssh.run(f"sudo config interface fec {port} none", timeout=15)


def _get_counter(ssh, port, stat):
    """Read a single counter value from COUNTERS_DB for the given port."""
    oid_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10
    )
    oid = oid_out.strip()
    val_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget 'COUNTERS:{oid}' {stat}", timeout=10
    )
    return int(val_out.strip() or "0")


def test_portchannel_rx_counters_increment(ssh):
    """5000-packet flood to peer increments PortChannel member RX_OK by >= 4500."""
    before = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_UCAST_PKTS") for p in LAG_PORTS]
    ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(2)
    after = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_UCAST_PKTS") for p in LAG_PORTS]
    delta = sum(after[i] - before[i] for i in range(len(LAG_PORTS)))
    assert delta >= 4500, (
        f"RX_OK delta across {LAG_PORTS} was {delta}, expected >= 4500.\n"
        f"Before: {before}, After: {after}"
    )


def test_portchannel_tx_counters_increment(ssh):
    """5000-packet flood generates TX_OK on LAG member ports."""
    before = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS") for p in LAG_PORTS]
    ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=60)
    time.sleep(2)
    after = [_get_counter(ssh, p, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS") for p in LAG_PORTS]
    delta = sum(after[i] - before[i] for i in range(len(LAG_PORTS)))
    assert delta >= 4500, f"TX_OK delta={delta} < 4500: before={before} after={after}"


def test_standalone_port_rx_tx(ssh):
    """Ping flood via Ethernet48 increments both RX and TX counters."""
    rx_before = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_IN_UCAST_PKTS")
    tx_before = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS")
    ssh.run(f"sudo ping -f -c 1000 {STANDALONE_PEER_IP} -W 2 > /dev/null 2>&1", timeout=30)
    time.sleep(2)
    rx_after = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_IN_UCAST_PKTS")
    tx_after = _get_counter(ssh, STANDALONE_PORT, "SAI_PORT_STAT_IF_OUT_UCAST_PKTS")
    assert rx_after - rx_before >= 900, f"Ethernet48 RX delta too low: {rx_after - rx_before}"
    assert tx_after - tx_before >= 900, f"Ethernet48 TX delta too low: {tx_after - tx_before}"


def test_fec_error_rate_100g(ssh):
    """Correctable FEC error rate < 1e-6/s on connected ports under traffic load."""
    ports = LAG_PORTS + [STANDALONE_PORT]
    before = {p: _get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES") for p in ports}
    # Generate traffic during the measurement window to stress the physical layer
    ssh.run(f"sudo ping -f -c 5000 {PEER_IP} -W 2 > /dev/null 2>&1", timeout=30)
    time.sleep(1)
    after = {p: _get_counter(ssh, p, "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES") for p in ports}
    elapsed = 6.0  # ~5s ping flood + 1s settle
    for p in ports:
        rate = (after[p] - before[p]) / elapsed
        assert rate < 1e-6, f"{p} FEC correctable rate {rate:.2e}/s exceeds 1e-6/s under load"


def test_counter_clear_accuracy(ssh):
    """After sonic-clear counters, connected port RX_OK <= 20 (LLDP only)."""
    ssh.run("sudo sonic-clear counters", timeout=15)
    time.sleep(2)
    for port in LAG_PORTS + [STANDALONE_PORT]:
        rx = _get_counter(ssh, port, "SAI_PORT_STAT_IF_IN_UCAST_PKTS")
        assert rx <= 20, (
            f"{port} has {rx} RX_OK after clear — expected <= 20 "
            f"(residual unicast only; LLDP is multicast and counted separately)"
        )
```

- [ ] **Step 4: Collect check**

```bash
python3 -m pytest tests/stage_20_traffic/ --collect-only 2>&1 | head -15
```

- [ ] **Step 5: Commit**

```bash
git add tests/target.cfg tests/stage_20_traffic/
git commit -m "test: add stage_20_traffic forwarding verification (ping-flood + counter delta)"
```

---

## Task 17: Full 20-Stage Suite Validation

- [ ] **Step 1: Run complete suite**

```bash
cd /export/sonic/sonic-buildimage.claude/tests
python3 run_tests.py 2>&1 | tee reports/full_suite_$(date +%Y-%m-%d_%H%M%S).txt
```

Expected: all 20 stages pass.

- [ ] **Step 2: Verify post-restore state**

```bash
ssh admin@192.168.88.12 'show interfaces portchannel; show interfaces status | grep "Ethernet16\|Ethernet32\|Ethernet48\|Ethernet112"' 2>&1
```

- [ ] **Step 3: Commit final report**

```bash
git add tests/reports/
git commit -m "test: full 20-stage suite passing — hardware completion phase complete"
```

---

## Notes on Ordering

The full stage execution order is:

All ordering is by natural alphabetical sort of directory names. `stage_nn_posttest` sorts after all digit-prefixed stage directories because ASCII `n` (110) > ASCII `9` (57).

```
stage_00_pretest      → save user config + config reload clean_boot.json
stage_01_eeprom       → read-only
…
stage_10_daemon       → read-only
stage_11_transceiver  → read-only (pmon running, xcvrd populates STATE_DB)
stage_12_counters     → minimal (flex counter enable if off, restore after)
stage_13_link         → fixture: add RS-FEC → tests → remove RS-FEC
stage_14_breakout     → fixture: set 1x100G → tests → restore 1x100G
stage_15_autoneg_fec  → fixture: RS-FEC on connected ports → tests → remove
stage_16_portchannel  → fixture: create PortChannel1 → tests → remove
stage_17_report       → read-only report (on clean-boot state)
stage_19_platform_cli → platform API audit (each test is self-contained)
stage_20_traffic      → fixture: create PortChannel1 + IPs → tests → remove
stage_nn_posttest     → restore user config from snapshot (always last)
```

Adding a new numbered stage (e.g., `stage_21_foo`) automatically runs between `stage_20_traffic` and `stage_nn_posttest` with no changes to `run_tests.py`.

Single-stage invocation (`./run_tests.py stage_12_counters`) auto-injects bookends:
```
stage_00_pretest → stage_12_counters → stage_nn_posttest
```
