"""Shared iperf3 pair definitions, runner, and sub-round scheduler.

Used by both:
  - tests/stage_23_throughput/test_throughput.py (30s, assertions, counter instrumentation)
  - tests/lib/report.py (10s, table output, no assertions)
"""

import json
import os

# ---------------------------------------------------------------------------
# Thresholds (bits/second)
# ---------------------------------------------------------------------------
THRESH_10G  = 8e9
THRESH_25G  = 20e9
THRESH_50G  = 40e9
THRESH_100G = 55e9   # CX6 Dx test nodes top out ~65 Gbps (CPU-limited)

# ---------------------------------------------------------------------------
# Pair definitions
# ---------------------------------------------------------------------------
# Format: (server_port, client_port, label, threshold_bps, parallel, extra_kwargs)
#
# Ordered within each round so the greedy sub-round scheduler groups
# non-conflicting hosts together.

ROUND1_PAIRS = [
    # Sub-round A: 100G (et6b3↔et6b1) + 10G (et7b3↔et25b1) — no host overlap
    ("Ethernet24", "Ethernet28", "100G↔100G", THRESH_100G, 16,
     {"zerocopy": True}),
    ("Ethernet66", "Ethernet67", "10G↔10G",   THRESH_10G,  5, {}),
    # Sub-round B: 50G (et6b1↔et6b3) + 25G (et6b3↔et7b1)
    ("Ethernet20", "Ethernet22", "50G↔50G",   THRESH_50G,  6,
     {"zerocopy": True}),
    ("Ethernet0",  "Ethernet80", "25G↔25G",   THRESH_25G,  5, {}),
]

ROUND2_PAIRS = [
    # Sub-round A: 100G↔50G (et6b3↔et6b1)
    ("Ethernet24", "Ethernet20", "100G↔50G",  THRESH_50G,  8,
     {"zerocopy": True}),
    # Sub-round B: 100G↔10G (et6b1↔et7b3) + 50G↔25G (et6b3↔et7b1) — no overlap
    ("Ethernet28", "Ethernet66", "100G↔10G",  THRESH_10G,  5, {}),
    ("Ethernet22", "Ethernet80", "50G↔25G",   THRESH_25G,  5, {}),
]

ALL_ROUNDS = [ROUND1_PAIRS, ROUND2_PAIRS]

# ---------------------------------------------------------------------------
# Sub-round scheduler
# ---------------------------------------------------------------------------

def schedule_subrounds(pairs):
    """Split pairs into sub-rounds so no physical host (mgmt_ip) appears more than once.

    Greedy: for each pair, if either mgmt_ip is already used in the current
    sub-round, start a new one.  Pair ordering matters — see definitions above.
    """
    subrounds = []
    current = []
    current_hosts = set()
    for p in pairs:
        srv_ip = p["hosts"][p["server_port"]]["mgmt_ip"]
        cli_ip = p["hosts"][p["client_port"]]["mgmt_ip"]
        if srv_ip in current_hosts or cli_ip in current_hosts:
            subrounds.append(current)
            current = []
            current_hosts = set()
        current.append(p)
        current_hosts.update([srv_ip, cli_ip])
    if current:
        subrounds.append(current)
    return subrounds


# ---------------------------------------------------------------------------
# Pair builder (forward / reverse)
# ---------------------------------------------------------------------------

def build_pairs(pair_defs, hosts, reverse=False):
    """Build pair dicts from tuple definitions.

    pair_defs: list of (srv_port, cli_port, label, threshold, parallel, extra_kw)
    hosts: dict mapping port name → {port, mgmt_ip, test_ip}
    """
    pairs = []
    for srv, cli, label, thresh, par, extra in pair_defs:
        if reverse:
            srv, cli = cli, srv
            label = f"{label} rev"
        pairs.append({
            "label": label, "server_port": srv, "client_port": cli,
            "threshold": thresh, "parallel": par, "hosts": hosts,
            **extra,
        })
    return pairs


# ---------------------------------------------------------------------------
# iperf3 pair runner
# ---------------------------------------------------------------------------

def _host_ssh(mgmt_ip, creds):
    """Return a connected paramiko SSHClient."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": mgmt_ip, "username": creds["ssh_user"], "timeout": 10}
    kf = creds.get("key_file")
    if kf:
        kw["key_filename"] = os.path.expanduser(kf)
    client.connect(**kw)
    return client


def _run(client, cmd, timeout=30):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    return out, err, rc


def run_iperf3_pair(label, server_test_ip, server_mgmt,
                    client_test_ip, client_mgmt, creds, threshold,
                    duration, parallel=5, port=5201,
                    zerocopy=False, window=None):
    """Run one iperf3 server↔client pair.

    Each pair uses a unique `port` so multiple instances can coexist on
    the same physical host.

    High-speed tuning:
      zerocopy=True  → --zerocopy (sendfile, avoids user→kernel copy)
      window="4M"    → -w 4M (larger TCP window for BDP)

    Returns (label, bits_per_second).
    Raises AssertionError if throughput < threshold.
    """
    srv = _host_ssh(server_mgmt, creds)
    cli = _host_ssh(client_mgmt, creds)
    try:
        _run(srv, f"pkill -f 'iperf3.*-p {port}' 2>/dev/null || true")
        _run(srv,
             f"nohup iperf3 -s -1 -B {server_test_ip} -p {port} </dev/null >/dev/null 2>&1 & "
             f"sleep 1; ss -tlnp 2>/dev/null | grep -q :{port} && echo LISTENING",
             timeout=5)
        client_cmd = (
            f"iperf3 -c {server_test_ip} -B {client_test_ip} "
            f"-t {duration} -P {parallel} -p {port}"
        )
        if zerocopy:
            client_cmd += " --zerocopy"
        if window:
            client_cmd += f" -w {window}"
        client_cmd += " --json"

        out, err, rc = _run(cli, client_cmd, timeout=duration + 15)
        if rc != 0:
            raise RuntimeError(f"[{label}] iperf3 client failed: {err.strip()[:200]}")
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        return label, bps
    finally:
        _run(srv, f"pkill -f 'iperf3.*-p {port}' 2>/dev/null || true")
        srv.close()
        cli.close()
