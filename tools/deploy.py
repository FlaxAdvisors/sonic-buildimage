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
import time

# Allow running as tools/deploy.py from repo root
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
sys.path.insert(0, _REPO_ROOT)

from tests.lib.ssh_client import SSHClient
from tools.tasks.cpu_affinity import CpuAffinityTask
from tools.tasks.system_tuning import SystemTuningTask
from tools.tasks.mgmt_vrf import MgmtVrfTask
from tools.tasks.breakout import BreakoutTask
from tools.tasks.portchannel import PortChannelTask
from tools.tasks.vlans import VlanTask
from tools.tasks.optical import OpticalTask

TASK_ORDER = [
    ("system_tuning", SystemTuningTask),
#    ("cpu_affinity",  CpuAffinityTask),
#    ("mgmt_vrf",      MgmtVrfTask),
    ("breakout",      BreakoutTask),
    ("portchannel",   PortChannelTask),
    ("vlans",         VlanTask),
    ("optical",       OpticalTask),
]

# Tasks at or after this index require the SONiC platform stack to be up
# (portsyncd must have populated PORT entries in config_db).  Tasks before
# it (system_tuning, cpu_affinity, mgmt_vrf) must NOT be gated — especially
# mgmt_vrf, which must run before the system is fully ready to restore SSH.
_SYSTEM_READY_TASK = "breakout"

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


def _wait_system_ready(ssh, timeout: int = 300) -> None:
    """Block until STATE_DB SYSTEM_READY|SYSTEM_STATE Status == UP.

    Polls every 5 s.  Returns immediately if already UP (common on re-runs).
    Exits with an error if the system does not become ready within `timeout` s.
    """
    cmd = "sonic-db-cli STATE_DB HGET 'SYSTEM_READY|SYSTEM_STATE' Status"
    deadline = time.time() + timeout
    first = True
    while True:
        out, _, _ = ssh.run(cmd, timeout=10)
        if out.strip() == "UP":
            if not first:
                print("  [system-ready] System is ready.", flush=True)
            return
        if first:
            print("  [system-ready] Waiting for SONiC to become ready "
                  f"(timeout {timeout}s)...", flush=True)
            first = False
        if time.time() >= deadline:
            sys.exit(
                f"ERROR: system did not reach ready state within {timeout}s. "
                "Check 'show system-health detail' on the switch."
            )
        time.sleep(5)


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

    # Tasks at or after _SYSTEM_READY_TASK in TASK_ORDER require the platform stack.
    _gate_idx = next(i for i, (n, _) in enumerate(TASK_ORDER) if n == _SYSTEM_READY_TASK)
    _port_tasks = {n for i, (n, _) in enumerate(TASK_ORDER) if i >= _gate_idx}
    needs_ready = any(name in _port_tasks for name, _ in tasks_to_run)
    system_ready_checked = False

    all_ok = True
    for name, task_cls in tasks_to_run:
        if needs_ready and not system_ready_checked and name in _port_tasks:
            _wait_system_ready(ssh)
            system_ready_checked = True
        ok = _run_task(name, task_cls, ssh, topology, dry_run=args.dry_run)
        if not ok:
            all_ok = False
            print(f"\nERROR: task {name!r} failed. Stopping.", flush=True)
            break

    if all_ok and not args.dry_run:
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
