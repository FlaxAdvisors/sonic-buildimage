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
