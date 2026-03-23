# L2 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three functional gaps (default config_db.json, throughput tests, --report coverage) and harden the I2C daemon architecture for production readiness.

**Architecture:** Four independent work streams: (1) 11 new report.py reporters for stages 09–12, 14–16, 19–21, 23; (2) a static config_db.json shipped with the platform .deb and installed on first boot via postinst; (3) a new stage_23_throughput/ pytest stage with 6 iperf3 tests; (4) I2C daemon hardening — bank-interleaved DOM reads then full daemon bus ownership, with INT_L and USB-CDC-Ethernet as investigation-gated follow-ons.

**Tech Stack:** Python 3, pytest, SSH (paramiko), iperf3, C (gcc), Debian packaging (dpkg), redis-cli, SONiC CLI (show/config), systemd

---

### Task 1: Section 3 — Report Expansion

Adds 11 new reporter functions to `tests/lib/report.py` and registers them in `REPORTERS`.

**Files:**
- Modify: `tests/lib/report.py`

- [ ] Step 1: Verify all 11 stage dirs exist: `ls tests/stage_09_cpld tests/stage_10_daemon tests/stage_11_transceiver tests/stage_12_counters tests/stage_14_breakout tests/stage_15_autoneg_fec tests/stage_16_portchannel tests/stage_19_platform_cli tests/stage_20_traffic tests/stage_21_lpmode`
  Expected: each directory listed without error.

- [ ] Step 2: Add `report_cpld` function to `tests/lib/report.py` (insert before the Registry section at the bottom):

```python
# ---------------------------------------------------------------------------
# Stage 09 — CPLD
# ---------------------------------------------------------------------------

def report_cpld(ssh):
    """Stage 09 — CPLD version, PSU present/pgood bits, LED register raw values."""
    sysfs = '/sys/bus/i2c/devices/1-0032'

    ver_raw, _, _ = ssh.run(f"cat {sysfs}/cpld_version 2>/dev/null || echo N/A")
    print(f"\n  CPLD version : {ver_raw.strip()}")

    psu_rows = []
    for slot in (1, 2):
        for attr, label in [('psu_present', 'present'), ('psu_power_good', 'pgood')]:
            val, _, rc = ssh.run(f"cat {sysfs}/{attr}{slot} 2>/dev/null")
            psu_rows.append((f"PSU{slot}", label, val.strip() if rc == 0 else "err"))
    _table(["PSU", "Signal", "Value"], psu_rows, title="PSU sysfs bits")

    led_rows = []
    for label, attr, reg in [("SYS1", 'led_sys1', '0x3e'), ("SYS2", 'led_sys2', '0x3f')]:
        val, _, rc = ssh.run(f"cat {sysfs}/{attr} 2>/dev/null")
        led_rows.append((label, reg, val.strip() if rc == 0 else "err"))
    _table(["LED", "Reg", "Raw"], led_rows, title="LED registers")
```

- [ ] Step 3: Add `report_daemon` function:

```python
# ---------------------------------------------------------------------------
# Stage 10 — I2C daemon health
# ---------------------------------------------------------------------------

def report_daemon(ssh):
    """Stage 10 — i2c-poller timer state and /run/wedge100s/ cache health."""
    timer_state, _, _ = ssh.run(
        "systemctl is-active wedge100s-i2c-poller.timer 2>/dev/null || echo unknown"
    )
    print(f"\n  i2c-poller timer : {timer_state.strip()}")

    last_trigger, _, _ = ssh.run(
        "systemctl show wedge100s-i2c-poller.timer "
        "--property=LastTriggerUSec --value 2>/dev/null || echo unknown"
    )
    print(f"  last trigger     : {last_trigger.strip()}")

    age_out, _, _ = ssh.run(
        "find /run/wedge100s -name 'sfp_*_present' -printf '%T@\\n' 2>/dev/null "
        "| sort -n | head -1"
    )
    if age_out.strip():
        import time as _time
        try:
            oldest = float(age_out.strip())
            age_s = int(_time.time() - oldest)
            print(f"  oldest present   : {age_s}s ago")
        except ValueError:
            pass
```

- [ ] Step 4: Add `report_transceiver` function:

```python
# ---------------------------------------------------------------------------
# Stage 11 — Transceiver STATE_DB
# ---------------------------------------------------------------------------

def report_transceiver(ssh):
    """Stage 11 — TRANSCEIVER_INFO and TRANSCEIVER_DOM_SENSOR from STATE_DB."""
    info_out, _, _ = ssh.run(
        "redis-cli -n 6 KEYS 'TRANSCEIVER_INFO|*' 2>/dev/null | sort", timeout=20
    )
    ports = [l.split('|', 1)[1] for l in info_out.splitlines() if '|' in l]

    if not ports:
        print("\n  No transceiver info in STATE_DB")
        return

    rows = []
    for port in ports[:16]:
        vendor, _, _ = ssh.run(
            f"redis-cli -n 6 HGET 'TRANSCEIVER_INFO|{port}' vendor_name 2>/dev/null"
        )
        pn, _, _ = ssh.run(
            f"redis-cli -n 6 HGET 'TRANSCEIVER_INFO|{port}' vendor_part_number 2>/dev/null"
        )
        temp, _, _ = ssh.run(
            f"redis-cli -n 6 HGET 'TRANSCEIVER_DOM_SENSOR|{port}' temperature 2>/dev/null"
        )
        rows.append((port, vendor.strip() or "—", pn.strip() or "—", temp.strip() or "—"))

    _table(["Port", "Vendor", "Part Number", "Temp (°C)"], rows, title=f"Transceivers (first {len(rows)} of {len(ports)})")
    if len(ports) > 16:
        print(f"    … and {len(ports) - 16} more")
```

- [ ] Step 5: Add `report_counters` function:

```python
# ---------------------------------------------------------------------------
# Stage 12 — Interface counters
# ---------------------------------------------------------------------------

def report_counters(ssh):
    """Stage 12 — RX/TX packets and errors for link-up ports."""
    status_out, _, _ = ssh.run("show interfaces status 2>/dev/null", timeout=30)
    up_ports = []
    for line in status_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+', line)
        if m and ' up ' in line:
            up_ports.append(m.group(1))

    if not up_ports:
        print("\n  No link-up ports")
        return

    ctr_out, _, _ = ssh.run("show interfaces counters 2>/dev/null", timeout=30)
    rows = []
    for line in ctr_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)', line)
        if m and m.group(1) in up_ports:
            rows.append((m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)))

    _table(["Port", "RX_OK", "RX_ERR", "TX_OK", "TX_ERR"], rows, title="Link-up port counters")
```

- [ ] Step 6: Add `report_breakout` function:

```python
# ---------------------------------------------------------------------------
# Stage 14 — Breakout mode
# ---------------------------------------------------------------------------

def report_breakout(ssh):
    """Stage 14 — Active breakout mode per parent port from CONFIG_DB vs platform.json defaults."""
    _BREAKOUT_PARENTS = ["Ethernet0", "Ethernet64", "Ethernet80"]
    rows = []
    for parent in _BREAKOUT_PARENTS:
        # CONFIG_DB breakout mode
        mode_out, _, _ = ssh.run(
            f"redis-cli -n 4 HGET 'BREAKOUT_CFG|{parent}' brkout_mode 2>/dev/null"
        )
        mode = mode_out.strip() or "1x100G[40G]"
        # Count sub-ports in CONFIG_DB
        count_out, _, _ = ssh.run(
            f"redis-cli -n 4 KEYS 'PORT|{parent}*' 2>/dev/null | wc -l"
        )
        count = count_out.strip()
        rows.append((parent, mode, count))
    _table(["Parent Port", "Breakout Mode", "Sub-port Count"], rows, title="Breakout configuration")
```

- [ ] Step 7: Add `report_autoneg_fec` function:

```python
# ---------------------------------------------------------------------------
# Stage 15 — Autoneg / FEC
# ---------------------------------------------------------------------------

def report_autoneg_fec(ssh):
    """Stage 15 — FEC mode and autoneg state for connected ports."""
    status_out, _, _ = ssh.run("show interfaces status 2>/dev/null", timeout=30)
    rows = []
    for line in status_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+[\d,]+\s+(\S+)\s+\d+\s+(\S+)\s+\S+\s+(\S+)', line)
        if m:
            port, speed, fec, oper = m.group(1), m.group(2), m.group(3), m.group(4)
            if oper == 'up':
                rows.append((port, speed, fec, oper))

    _table(["Port", "Speed", "FEC", "Oper"], rows, title="FEC and autoneg (link-up ports)")
```

- [ ] Step 8: Add `report_portchannel` function:

```python
# ---------------------------------------------------------------------------
# Stage 16 — PortChannel
# ---------------------------------------------------------------------------

def report_portchannel(ssh):
    """Stage 16 — PortChannel1 members, LACP state, VLAN membership."""
    pc_out, _, _ = ssh.run("show interfaces portchannel 2>/dev/null", timeout=20)
    print(f"\n  PortChannel state:\n")
    for line in pc_out.strip().splitlines()[:20]:
        print(f"    {line}")

    lacp_out, _, rc = ssh.run("teamdctl PortChannel1 state 2>/dev/null || echo N/A")
    if rc == 0 and lacp_out.strip() != "N/A":
        import json as _json
        try:
            state = _json.loads(lacp_out)
            ports = state.get("ports", {})
            rows = [(p, d.get("runner", {}).get("selected", "?"), d.get("link", {}).get("up", "?"))
                    for p, d in sorted(ports.items())]
            _table(["Member", "LACP Selected", "Link Up"], rows, title="LACP member state")
        except Exception:
            print(f"\n  teamdctl output: {lacp_out.strip()[:200]}")

    vlan_out, _, _ = ssh.run(
        "redis-cli -n 4 SMEMBERS 'VLAN_MEMBER|Vlan999' 2>/dev/null"
    )
    print(f"\n  Vlan999 members: {vlan_out.strip() or '(none)'}")
```

- [ ] Step 9: Add `report_platform_cli` function:

```python
# ---------------------------------------------------------------------------
# Stage 19 — Platform CLI
# ---------------------------------------------------------------------------

def report_platform_cli(ssh):
    """Stage 19 — Base MAC, reboot cause, CPLD/BIOS version, watchdog status."""
    rows = []

    mac_out, _, _ = ssh.run("show platform syseeprom 2>/dev/null | grep 'Base MAC' || echo N/A")
    rows.append(("Base MAC", mac_out.strip()))

    reboot_out, _, _ = ssh.run("show reboot-cause 2>/dev/null || echo N/A")
    rows.append(("Reboot cause", reboot_out.strip()[:80]))

    cpld_out, _, _ = ssh.run(
        "cat /sys/bus/i2c/devices/1-0032/cpld_version 2>/dev/null || echo N/A"
    )
    rows.append(("CPLD version", cpld_out.strip()))

    bios_out, _, _ = ssh.run("sudo dmidecode -s bios-version 2>/dev/null || echo N/A")
    rows.append(("BIOS version", bios_out.strip()))

    wd_out, _, _ = ssh.run("show platform watchdog 2>/dev/null || echo N/A")
    rows.append(("Watchdog", wd_out.strip()[:80]))

    _table(["Item", "Value"], rows, title="Platform CLI summary")
```

- [ ] Step 10: Add `report_traffic` function:

```python
# ---------------------------------------------------------------------------
# Stage 20 — Traffic counters
# ---------------------------------------------------------------------------

def report_traffic(ssh):
    """Stage 20 — TX/RX counter deltas for Ethernet16/32 from COUNTERS_DB."""
    _TRAFFIC_PORTS = ["Ethernet16", "Ethernet32"]

    name_map_out, _, _ = ssh.run(
        "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP 2>/dev/null", timeout=15
    )
    name_map = {}
    tokens = name_map_out.split()
    for i in range(0, len(tokens) - 1, 2):
        name_map[tokens[i]] = tokens[i + 1]

    rows = []
    for port in _TRAFFIC_PORTS:
        oid = name_map.get(port)
        if not oid:
            rows.append((port, "OID not found", "—", "—", "—"))
            continue
        rx_out, _, _ = ssh.run(
            f"redis-cli -n 2 HGET COUNTERS:{oid} SAI_PORT_STAT_IF_IN_UCAST_PKTS 2>/dev/null"
        )
        tx_out, _, _ = ssh.run(
            f"redis-cli -n 2 HGET COUNTERS:{oid} SAI_PORT_STAT_IF_OUT_UCAST_PKTS 2>/dev/null"
        )
        rows.append((port, oid[:16] + "…", rx_out.strip() or "0", tx_out.strip() or "0", "snapshot"))

    _table(["Port", "OID (truncated)", "RX ucast pkts", "TX ucast pkts", "Note"],
           rows, title="Traffic counters (COUNTERS_DB snapshot)")
    print("\n  Note: run 'show interfaces counters' for human-readable deltas")
```

- [ ] Step 11: Add `report_lpmode` function:

```python
# ---------------------------------------------------------------------------
# Stage 21 — LP_MODE
# ---------------------------------------------------------------------------

def report_lpmode(ssh):
    """Stage 21 — LP_MODE state per installed SFP from /run/wedge100s/ cache."""
    rows = []
    for idx in range(32):
        present_raw, _, _ = ssh.run(
            f"cat /run/wedge100s/sfp_{idx}_present 2>/dev/null || echo -"
        )
        present = present_raw.strip()
        if present != "1":
            continue
        lp_raw, _, _ = ssh.run(
            f"cat /run/wedge100s/sfp_{idx}_lpmode 2>/dev/null || echo -"
        )
        lp = "asserted" if lp_raw.strip() == "1" else "deasserted"
        rows.append((f"port {idx}", "Yes", lp))

    if not rows:
        print("\n  No present ports with lpmode cache files")
        return
    _table(["Port", "Present", "LP_MODE"], rows, title="LP_MODE state")
```

- [ ] Step 12: Add `report_throughput` placeholder function:

```python
# ---------------------------------------------------------------------------
# Stage 23 — Throughput (placeholder)
# ---------------------------------------------------------------------------

def report_throughput(ssh):
    """Stage 23 — Throughput placeholder."""
    print("\n  Run: pytest tests/stage_23_throughput -v  for live throughput results")
```

- [ ] Step 13: Update the `REPORTERS` dict at the bottom of `tests/lib/report.py` to add all 11 new entries:

```python
REPORTERS = {
    "stage_01_eeprom":      report_eeprom,
    "stage_02_system":      report_system,
    "stage_03_platform":    report_platform,
    "stage_04_thermal":     report_thermal,
    "stage_05_fan":         report_fan,
    "stage_06_psu":         report_psu,
    "stage_07_qsfp":        report_qsfp,
    "stage_08_led":         report_led,
    "stage_09_cpld":        report_cpld,
    "stage_10_daemon":      report_daemon,
    "stage_11_transceiver": report_transceiver,
    "stage_12_counters":    report_counters,
    "stage_13_link":        report_link,
    "stage_14_breakout":    report_breakout,
    "stage_15_autoneg_fec": report_autoneg_fec,
    "stage_16_portchannel": report_portchannel,
    "stage_19_platform_cli": report_platform_cli,
    "stage_20_traffic":     report_traffic,
    "stage_21_lpmode":      report_lpmode,
    "stage_23_throughput":  report_throughput,
}
```

- [ ] Step 14: Verify syntax: `python3 -c "from tests.lib import report; print(len(report.REPORTERS), 'reporters')"` (run from buildimage root).
  Expected: `20 reporters`

- [ ] Step 15: Smoke test one reporter against live hardware: `cd tests && python3 run_tests.py --report stage_09_cpld`
  Expected: CPLD version line and PSU/LED tables appear without exceptions.

- [ ] Step 16: Commit.

---

### Task 2: Section 1 — Default config_db.json

Creates a minimal safe baseline config_db.json and installs it on first boot via the platform .deb postinst.

**Files:**
- Create: `device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json`
- Modify: `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`

- [ ] Step 1: Verify installer hook path by checking what runs on first boot with no config_db.json:
  `grep -n 'config_db\|generate_config\|factory\|initialization' files/image_config/config-setup/config-setup | head -20`
  Confirm that `do_config_initialization()` runs `generate_config factory` only if `/etc/sonic/config_db.json` is absent. The postinst copy (with existence guard) pre-empts this path.

- [ ] Step 2: Create `device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json` with the following content:

```json
{
    "DEVICE_METADATA": {
        "localhost": {
            "hostname": "sonic",
            "platform": "x86_64-accton_wedge100s_32x-r0",
            "hwsku": "Accton-WEDGE100S-32X",
            "mac": "00:00:00:00:00:00",
            "default_bgp_status": "down",
            "type": "LeafRouter"
        }
    },
    "FEATURE": {
        "bgp": {
            "state": "disabled",
            "auto_restart": "disabled",
            "has_per_asic_scope": "False",
            "has_global_scope": "True",
            "has_timer": "False"
        }
    },
    "MGMT_VRF_CONFIG": {
        "vrf_global": {
            "mgmtVrfEnabled": "true"
        }
    },
    "PORT": {
        "Ethernet0":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet4":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet8":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet12":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet16":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet20":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet24":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet28":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet32":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet36":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet40":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet44":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet48":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet52":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet56":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet60":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet64":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet68":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet72":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet76":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet80":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet84":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet88":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet92":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet96":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet100": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet104": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet108": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet112": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet116": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet120": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet124": {"speed": "100000", "fec": "rs", "admin_status": "up"}
    }
}
```

- [ ] Step 3: Validate JSON: `python3 -c "import json; d=json.load(open('device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json')); print(len(d['PORT']), 'ports,', d['DEVICE_METADATA']['localhost']['default_bgp_status'])"`
  Expected: `32 ports, down`

- [ ] Step 4: Add the following block to `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`, immediately after the port_breakout_config_db.json block (after line 59 of the current postinst):

```bash
# Install factory default config_db.json to /etc/sonic/ only on first install
# (i.e. when /etc/sonic/config_db.json does not yet exist).  This pre-empts
# the sonic-cfggen T0 topology generation that would otherwise flood every
# port with BGP neighbors and ARP traffic on first boot.
DEVICE_DIR="/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
FACTORY_CFG="$DEVICE_DIR/config_db.json"
TARGET_CFG="/etc/sonic/config_db.json"
if [ -f "$FACTORY_CFG" ] && [ ! -f "$TARGET_CFG" ]; then
    cp "$FACTORY_CFG" "$TARGET_CFG"
    echo "wedge100s postinst: installed factory config_db.json to /etc/sonic/"
fi
```

- [ ] Step 5: Verify postinst syntax: `bash -n platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`
  Expected: no output (syntax OK).

- [ ] Step 6: On the live hardware target, verify the postinst logic would fire correctly:
  ```
  ssh admin@192.168.88.12 test -f /etc/sonic/config_db.json && echo EXISTS || echo ABSENT
  ```
  This is currently ABSENT on a freshly imaged switch (normal for our test target which already has a deploy.py config). We are verifying the file exists in the source; actual fresh-install testing requires a full image rebuild and ONIE re-flash.

- [ ] Step 7: Commit.

---

### Task 3: Section 2 — Phase 23 Throughput Tests

Creates the `tests/stage_23_throughput/` pytest stage with 6 iperf3 throughput tests.

**Files:**
- Create: `tests/stage_23_throughput/__init__.py`
- Create: `tests/stage_23_throughput/conftest.py`
- Create: `tests/stage_23_throughput/test_throughput.py`
- Modify: `tests/STAGED_PHASES.md`

- [ ] Step 1: Create `tests/stage_23_throughput/__init__.py` (empty file).

- [ ] Step 2: Create `tests/stage_23_throughput/conftest.py`:

```python
"""Stage 23 conftest — fixtures for host SSH connections and iperf3 availability."""

import configparser
import json
import os
import socket
import subprocess

import pytest

TOPOLOGY_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'tools', 'topology.json')
TARGET_CFG_DEFAULT = os.path.join(os.path.dirname(__file__), '..', 'target.cfg')


def _load_topology():
    with open(TOPOLOGY_PATH) as f:
        return json.load(f)


def _load_target_cfg(cfg_path):
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return cfg


def _host_reachable(mgmt_ip, ssh_user, key_file, timeout=5):
    """Return True if we can open an SSH connection to mgmt_ip."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {"hostname": mgmt_ip, "username": ssh_user, "timeout": timeout}
        if key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(key_file)
        client.connect(**connect_kwargs)
        client.close()
        return True
    except Exception:
        return False


def _iperf3_available(mgmt_ip, ssh_user, key_file):
    """Return True if iperf3 binary is present on the remote host."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {"hostname": mgmt_ip, "username": ssh_user, "timeout": 10}
        if key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(key_file)
        client.connect(**connect_kwargs)
        _, stdout, _ = client.exec_command("which iperf3 2>/dev/null; echo $?")
        rc = int(stdout.read().decode().strip().splitlines()[-1])
        client.close()
        return rc == 0
    except Exception:
        return False


def _run_on_host(mgmt_ip, ssh_user, key_file, cmd, timeout=30):
    """Run cmd on mgmt_ip via SSH; return (stdout, stderr, returncode)."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {"hostname": mgmt_ip, "username": ssh_user, "timeout": 10}
    if key_file:
        connect_kwargs["key_filename"] = os.path.expanduser(key_file)
    client.connect(**connect_kwargs)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    client.close()
    return out, err, rc


@pytest.fixture(scope="session")
def topology():
    return _load_topology()


@pytest.fixture(scope="session")
def host_ssh_creds(request):
    cfg_path = request.config.getoption("--target-cfg", default=TARGET_CFG_DEFAULT)
    cfg = _load_target_cfg(cfg_path)
    ssh_user = cfg.get("hosts", "ssh_user", fallback="flax")
    key_file  = cfg.get("hosts", "key_file",  fallback="~/.ssh/id_rsa")
    return {"ssh_user": ssh_user, "key_file": key_file}


@pytest.fixture(scope="session")
def host_by_port(topology):
    """Dict mapping port name → host entry from topology.json."""
    return {h["port"]: h for h in topology.get("hosts", [])}
```

- [ ] Step 3: Create `tests/stage_23_throughput/test_throughput.py`:

```python
"""Stage 23 — Throughput verification via iperf3.

Tests:
  test_throughput_10g              Ethernet66 ↔ Ethernet67  ≥ 8 Gbps
  test_throughput_25g_pair1        Ethernet80 ↔ Ethernet81  ≥ 20 Gbps
  test_throughput_25g_pair2        Ethernet0  ↔ Ethernet1   ≥ 20 Gbps (skip if Ethernet1 dark)
  test_throughput_cross_qsfp       Ethernet66 ↔ Ethernet80  ≥ 8 Gbps  (bottleneck 10G)
  test_throughput_100g_eth48       Ethernet48 ↔ EOS Et15/1  ≥ 90 Gbps
  test_throughput_100g_eth112      Ethernet112 ↔ EOS Et16/1 ≥ 90 Gbps

All tests skip (not fail) when: iperf3 absent, host SSH unreachable, EOS iperf3 absent.
"""

import json
import os
import pytest

# EOS SSH coordinates — direct, no jump host needed when Po1 carries no IP.
EOS_HOST    = "192.168.88.14"
EOS_USER    = "admin"
EOS_PASSWD  = "0penSesame"

# SONiC switch SSH — we run iperf3 client from the switch side for 100G tests.
SONIC_HOST  = "192.168.88.12"
SONIC_USER  = "admin"

# Temporary /30 subnet for 100G switch-to-switch tests.
SONIC_TEMP_IP_ETH48   = "10.99.48.1/30"
EOS_TEMP_IP_ETH48     = "10.99.48.2"
SONIC_TEMP_IP_ETH112  = "10.99.112.1/30"
EOS_TEMP_IP_ETH112    = "10.99.112.2"

# Thresholds in bits/second
THRESH_10G  = 8e9
THRESH_25G  = 20e9
THRESH_100G = 90e9

# iperf3 test duration seconds
IPERF_DURATION = 10


def _host_ssh(mgmt_ip, creds):
    """Return a connected paramiko SSHClient or raise on failure."""
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


def _host_reachable(mgmt_ip, creds):
    try:
        c = _host_ssh(mgmt_ip, creds)
        c.close()
        return True
    except Exception:
        return False


def _iperf3_on_host(mgmt_ip, creds):
    try:
        c = _host_ssh(mgmt_ip, creds)
        out, _, rc = _run(c, "which iperf3 2>/dev/null; echo exit:$?")
        c.close()
        return "exit:0" in out
    except Exception:
        return False


def _run_iperf3_pair(server_ip, server_mgmt, client_mgmt, creds, threshold):
    """Start iperf3 server on server_mgmt, run client from client_mgmt to server_ip.

    Returns bits_per_second (float) from client sum_received.
    Raises AssertionError if throughput < threshold.
    """
    srv = _host_ssh(server_mgmt, creds)
    cli = _host_ssh(client_mgmt, creds)
    try:
        # Kill any stale iperf3 server
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        # Start server in background
        _run(srv, "nohup iperf3 -s -1 -D 2>/dev/null &", timeout=5)
        import time; time.sleep(1)
        # Run client
        out, err, rc = _run(cli,
            f"iperf3 -c {server_ip} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15)
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= threshold, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {threshold/1e9:.0f} Gbps"
        )
        return bps
    finally:
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        srv.close()
        cli.close()


# ── Host-to-host tests ──────────────────────────────────────────────────────

def test_throughput_10g(host_by_port, host_ssh_creds):
    """Ethernet66 ↔ Ethernet67 via VLAN 10; threshold ≥ 8 Gbps."""
    h66 = host_by_port.get("Ethernet66")
    h67 = host_by_port.get("Ethernet67")
    if not h66 or not h67:
        pytest.skip("Ethernet66 or Ethernet67 not in topology.json")

    if not _host_reachable(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h66['mgmt_ip']} (port Ethernet66) not reachable via SSH")
    if not _host_reachable(h67["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h67['mgmt_ip']} (port Ethernet67) not reachable via SSH")
    if not _iperf3_on_host(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h66['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h67["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h67['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h66["test_ip"], h66["mgmt_ip"], h67["mgmt_ip"],
                            host_ssh_creds, THRESH_10G)
    print(f"\n  10G pair throughput: {bps/1e9:.2f} Gbps")


def test_throughput_25g_pair1(host_by_port, host_ssh_creds):
    """Ethernet80 ↔ Ethernet81 via VLAN 10; threshold ≥ 20 Gbps."""
    h80 = host_by_port.get("Ethernet80")
    h81 = host_by_port.get("Ethernet81")
    if not h80 or not h81:
        pytest.skip("Ethernet80 or Ethernet81 not in topology.json")

    if not _host_reachable(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h80['mgmt_ip']} (port Ethernet80) not reachable via SSH")
    if not _host_reachable(h81["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h81['mgmt_ip']} (port Ethernet81) not reachable via SSH")
    if not _iperf3_on_host(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h80['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h81["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h81['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h80["test_ip"], h80["mgmt_ip"], h81["mgmt_ip"],
                            host_ssh_creds, THRESH_25G)
    print(f"\n  25G pair1 throughput: {bps/1e9:.2f} Gbps")


def test_throughput_25g_pair2(host_by_port, host_ssh_creds):
    """Ethernet0 ↔ Ethernet1 via VLAN 10; threshold ≥ 20 Gbps.

    EXPECTED SKIP: Ethernet1 is a confirmed dark lane (see TODO.md).
    """
    h0 = host_by_port.get("Ethernet0")
    h1 = host_by_port.get("Ethernet1")
    if not h0 or not h1:
        pytest.skip("Ethernet0 or Ethernet1 not in topology.json")

    if not _host_reachable(h1["mgmt_ip"], host_ssh_creds):
        pytest.skip(
            f"Ethernet1 dark lane (see TODO.md) — test_throughput_25g_pair2 skipped"
        )
    if not _host_reachable(h0["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h0['mgmt_ip']} (port Ethernet0) not reachable via SSH")
    if not _iperf3_on_host(h0["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h0['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h1["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h1['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h0["test_ip"], h0["mgmt_ip"], h1["mgmt_ip"],
                            host_ssh_creds, THRESH_25G)
    print(f"\n  25G pair2 throughput: {bps/1e9:.2f} Gbps")


def test_throughput_cross_qsfp(host_by_port, host_ssh_creds):
    """Ethernet66 (10G) ↔ Ethernet80 (25G) cross-QSFP via VLAN 10; threshold ≥ 8 Gbps."""
    h66 = host_by_port.get("Ethernet66")
    h80 = host_by_port.get("Ethernet80")
    if not h66 or not h80:
        pytest.skip("Ethernet66 or Ethernet80 not in topology.json")

    if not _host_reachable(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h66['mgmt_ip']} (port Ethernet66) not reachable via SSH")
    if not _host_reachable(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"Host {h80['mgmt_ip']} (port Ethernet80) not reachable via SSH")
    if not _iperf3_on_host(h66["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h66['mgmt_ip']} — install iperf3 and retry")
    if not _iperf3_on_host(h80["mgmt_ip"], host_ssh_creds):
        pytest.skip(f"iperf3 not found on host {h80['mgmt_ip']} — install iperf3 and retry")

    bps = _run_iperf3_pair(h66["test_ip"], h66["mgmt_ip"], h80["mgmt_ip"],
                            host_ssh_creds, THRESH_10G)
    print(f"\n  Cross-QSFP throughput: {bps/1e9:.2f} Gbps")


# ── 100G switch-to-switch tests ─────────────────────────────────────────────

def _eos_ssh():
    """Return a connected paramiko SSHClient to EOS."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(EOS_HOST, username=EOS_USER, password=EOS_PASSWD, timeout=10)
    return client


def _sonic_ssh():
    """Return a connected paramiko SSHClient to SONiC switch."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SONIC_HOST, username=SONIC_USER, timeout=10)
    return client


def _iperf3_on_eos():
    try:
        c = _eos_ssh()
        out, _, rc = _run(c, "bash -c 'which iperf3 2>/dev/null; echo exit:$?'")
        c.close()
        return "exit:0" in out
    except Exception:
        return False


@pytest.fixture
def sonic_eth48_temp_ip(ssh):
    """Assign and teardown temp /30 IP on Ethernet48 for 100G test."""
    ssh.run(f"sudo config interface ip add Ethernet48 {SONIC_TEMP_IP_ETH48}", timeout=10)
    yield SONIC_TEMP_IP_ETH48.split('/')[0]
    ssh.run(f"sudo config interface ip remove Ethernet48 {SONIC_TEMP_IP_ETH48}", timeout=10)


@pytest.fixture
def eos_eth_temp_ip_48():
    """Assign and teardown temp IP on EOS Et15/1 for 100G test."""
    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'ip addr add {EOS_TEMP_IP_ETH48}/30 dev et15 2>/dev/null || true'")
        yield EOS_TEMP_IP_ETH48
    finally:
        _run(eos, f"bash -c 'ip addr del {EOS_TEMP_IP_ETH48}/30 dev et15 2>/dev/null || true'")
        eos.close()


@pytest.fixture
def sonic_eth112_temp_ip(ssh):
    """Assign and teardown temp /30 IP on Ethernet112 for 100G test."""
    ssh.run(f"sudo config interface ip add Ethernet112 {SONIC_TEMP_IP_ETH112}", timeout=10)
    yield SONIC_TEMP_IP_ETH112.split('/')[0]
    ssh.run(f"sudo config interface ip remove Ethernet112 {SONIC_TEMP_IP_ETH112}", timeout=10)


@pytest.fixture
def eos_eth_temp_ip_112():
    """Assign and teardown temp IP on EOS Et16/1 for 100G test."""
    eos = _eos_ssh()
    try:
        _run(eos, f"bash -c 'ip addr add {EOS_TEMP_IP_ETH112}/30 dev et16 2>/dev/null || true'")
        yield EOS_TEMP_IP_ETH112
    finally:
        _run(eos, f"bash -c 'ip addr del {EOS_TEMP_IP_ETH112}/30 dev et16 2>/dev/null || true'")
        eos.close()


def test_throughput_100g_eth48(ssh, sonic_eth48_temp_ip, eos_eth_temp_ip_48):
    """Ethernet48 ↔ EOS Et15/1 at 100G; threshold ≥ 90 Gbps."""
    if not _iperf3_on_eos():
        pytest.skip("iperf3 not found in EOS bash — cannot run 100G switch-to-switch test")

    eos = _eos_ssh()
    try:
        # Kill stale, start server on EOS
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null; nohup iperf3 -s -1 -D 2>/dev/null &'")
        import time; time.sleep(1)

        # Run iperf3 client from SONiC switch toward EOS
        out, err, rc = ssh.run(
            f"iperf3 -c {eos_eth_temp_ip_48} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15
        )
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= THRESH_100G, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {THRESH_100G/1e9:.0f} Gbps"
        )
        print(f"\n  Ethernet48↔EOS throughput: {bps/1e9:.2f} Gbps")
    finally:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null || true'")
        eos.close()


def test_throughput_100g_eth112(ssh, sonic_eth112_temp_ip, eos_eth_temp_ip_112):
    """Ethernet112 ↔ EOS Et16/1 at 100G; threshold ≥ 90 Gbps."""
    if not _iperf3_on_eos():
        pytest.skip("iperf3 not found in EOS bash — cannot run 100G switch-to-switch test")

    eos = _eos_ssh()
    try:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null; nohup iperf3 -s -1 -D 2>/dev/null &'")
        import time; time.sleep(1)

        out, err, rc = ssh.run(
            f"iperf3 -c {eos_eth_temp_ip_112} -t {IPERF_DURATION} --json",
            timeout=IPERF_DURATION + 15
        )
        assert rc == 0, f"iperf3 client failed: {err.strip()[:200]}"
        data = json.loads(out)
        bps = data["end"]["sum_received"]["bits_per_second"]
        assert bps >= THRESH_100G, (
            f"Throughput {bps/1e9:.2f} Gbps < threshold {THRESH_100G/1e9:.0f} Gbps"
        )
        print(f"\n  Ethernet112↔EOS throughput: {bps/1e9:.2f} Gbps")
    finally:
        _run(eos, "bash -c 'pkill -f iperf3 2>/dev/null || true'")
        eos.close()
```

- [ ] Step 4: Verify the new stage is picked up by run_tests.py: `cd tests && python3 run_tests.py --list | grep stage_23`
  Expected: `stage_23_throughput` appears.

- [ ] Step 5: Run the stage with the live target to confirm skip behavior (iperf3 likely absent on some hosts):
  `cd tests && pytest stage_23_throughput -v`
  Expected: tests skip (not fail) for missing iperf3 or unreachable hosts. No traceback on fixture teardown.

- [ ] Step 6: Update `tests/STAGED_PHASES.md` — change Phase 23 status from PENDING to COMPLETE and update the overall pass rate to reflect the new test count (add 6 tests, show how many skip vs pass based on actual run).

- [ ] Step 7: Commit.

---

### Task 4: Cleanup

Remove stale editor backup file.

**Files:**
- Delete: `tools/tasks/mgmt_vrf.py~`

- [ ] Step 1: `rm tools/tasks/mgmt_vrf.py~`
- [ ] Step 2: `git status tools/tasks/` — confirm the `~` file is gone and `mgmt_vrf.py` is unchanged.
- [ ] Step 3: Commit.

---

### Task 5: Section 4 Investigations — INT_L GPIO and USB-CDC-Ethernet

Hardware-only investigation. No code changes. Results determine whether Tasks 7 and 8 are implemented.

- [ ] Step 1 (4a — INT_L GPIO): SSH to switch: `ssh admin@192.168.88.12`

- [ ] Step 2: List available GPIO chips: `sudo gpiodetect 2>/dev/null || ls /sys/class/gpio/gpiochip*/label 2>/dev/null`

- [ ] Step 3: Count GPIO lines: `sudo gpioinfo 2>/dev/null | head -40`

- [ ] Step 4: Check ONL platform source for any GPIO used for QSFP INT: `grep -r "INT\|gpio" /export/sonic/OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/ 2>/dev/null | grep -iv "license\|copyright" | head -20`

- [ ] Step 5: Check `/sys/class/gpio/` for any chip labeled pca9535 or related to interrupt:
  `ssh admin@192.168.88.12 'ls /sys/class/gpio/'`

- [ ] Step 6: Document result in `tests/notes/int_l_gpio_investigation.md`:
  - If no host CPU GPIO wired to PCA9535 INT: document "No host GPIO path for INT_L — one-shot daemon architecture preserved; presence polling moved to 10s timer (Task 6b)". Task 7 (4a implementation) is CANCELLED.
  - If GPIO confirmed: document chip, line number, edge type; Task 7 proceeds.

- [ ] Step 7 (4d — USB-CDC-Ethernet): From dev host: `lsusb -v 2>/dev/null | grep -A 30 "OpenBMC\|ASPEED\|1d6b:0002\|bLength.*Configuration" | head -50`

- [ ] Step 8: SSH to SONiC switch, check USB device: `ssh admin@192.168.88.12 'lsusb -v 2>/dev/null | grep -A 20 "ttyACM\|CDC\|Ethernet\|BMC" | head -60'`

- [ ] Step 9: SSH to BMC: `ssh root@192.168.88.13 'ls /sys/class/udc/ 2>/dev/null; cat /proc/net/if_inet6 2>/dev/null | head -5; ls /sys/bus/platform/drivers/gadget* 2>/dev/null; systemctl list-units | grep -i "usb\|gadget\|cdc" 2>/dev/null'`

- [ ] Step 10: Document result in `tests/notes/bmc_usb_cdc_investigation.md`:
  - If CDC-ECM interface available: document interface details and private IP assignment plan. Task 8 (implementation) proceeds.
  - If CDC-ECM not available: document "USB-CDC-ACM only; CDC-ECM not available — TTY blocking path is permanent constraint". Task 8 is CANCELLED.

- [ ] Step 11: Commit notes.

---

### Task 6: Section 4c — Bank-Interleaved DOM Reads

Halves I2C transactions per DOM refresh cycle by alternating which bank-group refreshes each tick.

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`

- [ ] Step 1: Add the following constants and function to `sfp.py`, immediately after the `_DOM_LAST_REFRESH` definition (after line 94). Note: `_DOM_CACHE_TTL = 10` already exists — REPLACE that line with `_DOM_CACHE_TTL = 20`:

```python
_DOM_CACHE_TTL      = 20              # seconds: max staleness per port (was 10)
_BANK_GROUP_A       = set(range(0, 16))   # mux 0x70 + 0x71 (Ethernet0–Ethernet60)
_BANK_GROUP_B       = set(range(16, 32))  # mux 0x72 + 0x73 (Ethernet64–Ethernet124)
_tick_counter       = 0               # incremented per xcvrd process; not persisted


def _dom_refresh_eligible(port_index: int) -> bool:
    """Return True if this port is in the active bank-group this tick.

    Bank-group A (ports 0–15) refreshes on even ticks; bank-group B (ports 16–31)
    on odd ticks.  Each port refreshes at most every 20s (2 ticks × 10s interval).
    """
    in_group_a = port_index < 16
    even_tick  = (_tick_counter % 2 == 0)
    return in_group_a == even_tick
```

- [ ] Step 2: In `read_eeprom()`, locate the DOM refresh eligibility check (currently `if offset < 128 and (time.monotonic() - _DOM_LAST_REFRESH[self._port]) > _DOM_CACHE_TTL:`). Change it to also check `_dom_refresh_eligible(self._port)`:

  Old:
  ```python
  if offset < 128 and (time.monotonic() - _DOM_LAST_REFRESH[self._port]) > _DOM_CACHE_TTL:
  ```

  New:
  ```python
  if offset < 128 and (time.monotonic() - _DOM_LAST_REFRESH[self._port]) > _DOM_CACHE_TTL and _dom_refresh_eligible(self._port):
  ```

- [ ] Step 3: Add tick counter increment. In `read_eeprom()`, after the `_DOM_LAST_REFRESH[self._port] = time.monotonic()` line, add:
  ```python
  global _tick_counter
  _tick_counter += 1
  ```
  The counter increments each time any port completes a DOM refresh. The alternation effect is approximate and still achieves ~50% traffic reduction per tick with no need for perfect synchronization.

- [ ] Step 4: Verify syntax:
  ```
  python3 -c "import sys; sys.path.insert(0, 'platform/broadcom/sonic-platform-modules-accton/wedge100s-32x'); from sonic_platform import sfp; print('_DOM_CACHE_TTL =', sfp._DOM_CACHE_TTL); print('bank A eligible at tick 0:', sfp._dom_refresh_eligible(0)); print('bank B eligible at tick 0:', sfp._dom_refresh_eligible(16))"
  ```
  Expected: `_DOM_CACHE_TTL = 20`, `bank A eligible at tick 0: True`, `bank B eligible at tick 0: False`

- [ ] Step 5: Deploy and verify on hardware:
  ```bash
  scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py admin@192.168.88.12:~
  ssh admin@192.168.88.12 'sudo cp ~/sfp.py /usr/lib/python3/dist-packages/sonic_platform/sfp.py && docker exec pmon pip3 install --quiet --force-reinstall /usr/share/sonic/platform/sonic_platform-1.0-py3-none-any.whl 2>/dev/null; docker exec pmon supervisorctl restart xcvrd'
  ssh admin@192.168.88.12 'sleep 5 && redis-cli -n 6 HGET "TRANSCEIVER_DOM_SENSOR|Ethernet48" temperature'
  ```
  Expected: a temperature value returned (confirming DOM reads still work).

- [ ] Step 6: Run stage_11_transceiver to confirm no regression: `cd tests && pytest stage_11_transceiver -v`
  Expected: all tests pass.

- [ ] Step 7: Commit.

---

### Task 7: Section 4b — Full Daemon I2C Ownership

Extends the LP_MODE request/response file pattern to cover all pmon-initiated I2C writes (write_eeprom) and live DOM reads (_hardware_read_lower_page), eliminating direct smbus2 bus access from sfp.py.

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c`

**Note:** This is a significant architectural change. Implement and test incrementally:
- Phase A: add write protocol to sfp.py and daemon C; test write_eeprom via LP_MODE assert round-trip
- Phase B: migrate _hardware_read_lower_page to read request protocol; test DOM refresh
- Phase C: remove smbus2 imports from sfp.py once both are migrated

- [ ] Step 1: Add request/response path constants to `sfp.py` (after existing cache path constants):

```python
_WRITE_REQ  = '/run/wedge100s/sfp_{}_write_req'   # pmon → daemon: JSON {offset, length, data_hex}
_WRITE_ACK  = '/run/wedge100s/sfp_{}_write_ack'   # daemon → pmon: "ok" or "err:<msg>"
_READ_REQ   = '/run/wedge100s/sfp_{}_read_req'    # pmon → daemon: JSON {offset, length}
_READ_RESP  = '/run/wedge100s/sfp_{}_read_resp'   # daemon → pmon: hex-encoded bytes or "err:<msg>"
_WRITE_TIMEOUT_S = 5.0
_READ_TIMEOUT_S  = 5.0
```

- [ ] Step 2: Add helper `_wait_for_file(path, timeout_s)` to `sfp.py`:

```python
def _wait_for_file(path, timeout_s):
    """Poll path until it exists; return True on success, False on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False
```

- [ ] Step 3: Replace `write_eeprom()` body in sfp.py to use the request/response protocol:

```python
def write_eeprom(self, offset, num_bytes, write_buffer):
    """Write to QSFP EEPROM via daemon request file; wait for ack."""
    if num_bytes <= 0 or write_buffer is None:
        return False
    if not (0 <= offset < 256):
        return False

    req_path = _WRITE_REQ.format(self._port)
    ack_path = _WRITE_ACK.format(self._port)

    payload = {
        "offset": offset,
        "length": num_bytes,
        "data_hex": bytes(write_buffer[:num_bytes]).hex()
    }
    # Remove stale ack from any prior request.
    try:
        os.unlink(ack_path)
    except OSError:
        pass

    import json as _json
    try:
        tmp = req_path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(_json.dumps(payload))
        os.replace(tmp, req_path)
    except OSError:
        return False

    if not _wait_for_file(ack_path, _WRITE_TIMEOUT_S):
        # Timeout — daemon did not respond.
        try:
            os.unlink(req_path)
        except OSError:
            pass
        return False

    try:
        with open(ack_path) as f:
            result = f.read().strip()
        os.unlink(ack_path)
    except OSError:
        return False

    return result == "ok"
```

- [ ] Step 4: Replace `_hardware_read_lower_page()` body in sfp.py to use the read request/response protocol:

```python
def _hardware_read_lower_page(self):
    """Read lower page (bytes 0-127) from hardware via daemon read request file.

    Returns bytearray(128) on success, None on timeout or error.
    """
    req_path  = _READ_REQ.format(self._port)
    resp_path = _READ_RESP.format(self._port)

    payload = {"offset": 0, "length": 128}
    try:
        os.unlink(resp_path)
    except OSError:
        pass

    import json as _json
    try:
        tmp = req_path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(_json.dumps(payload))
        os.replace(tmp, req_path)
    except OSError:
        return None

    if not _wait_for_file(resp_path, _READ_TIMEOUT_S):
        try:
            os.unlink(req_path)
        except OSError:
            pass
        return None

    try:
        with open(resp_path) as f:
            result = f.read().strip()
        os.unlink(resp_path)
    except OSError:
        return None

    if result.startswith("err:"):
        return None
    try:
        data = bytes.fromhex(result)
        return bytearray(data) if len(data) == 128 else None
    except ValueError:
        return None
```

- [ ] Step 5: Add write request handling to `wedge100s-i2c-daemon.c`. After `poll_lpmode_hidraw()` call in `main()` and before `close(g_hidraw_fd)`, add a call to `poll_write_requests_hidraw()`. Write the function in C:

```c
/* ── poll_write_requests — process pending sfp_N_write_req files ─────────── */

static void poll_write_requests_hidraw(void)
{
    char req_path[128], ack_path[128], eeprom_path[128];
    char read_buf[4096];
    FILE *fp;

    for (int port = 0; port < NUM_SFPS; port++) {
        snprintf(req_path,    sizeof(req_path),    RUN_DIR "/sfp_%d_write_req",  port);
        snprintf(ack_path,    sizeof(ack_path),    RUN_DIR "/sfp_%d_write_ack",  port);
        snprintf(eeprom_path, sizeof(eeprom_path), RUN_DIR "/sfp_%d_eeprom",     port);

        fp = fopen(req_path, "r");
        if (!fp) continue;

        /* Read JSON payload */
        size_t n = fread(read_buf, 1, sizeof(read_buf) - 1, fp);
        fclose(fp);
        read_buf[n] = '\0';

        /* Minimal JSON parse: extract offset, length, data_hex */
        int offset = -1, length = -1;
        char data_hex[512] = {0};
        /* Use sscanf for simple parsing — no external JSON library needed */
        {
            char *p;
            p = strstr(read_buf, "\"offset\"");
            if (p) sscanf(p + 8, " : %d", &offset);
            p = strstr(read_buf, "\"length\"");
            if (p) sscanf(p + 8, " : %d", &length);
            p = strstr(read_buf, "\"data_hex\"");
            if (p) sscanf(p + 10, " : \"%511[^\"]\"", data_hex);
        }

        if (offset < 0 || length <= 0 || length > 256 || data_hex[0] == '\0') {
            write_str_file(ack_path, "err:bad_request");
            unlink(req_path);
            continue;
        }

        /* Convert hex string to bytes */
        uint8_t write_buf[256];
        int hex_len = (int)strlen(data_hex);
        if (hex_len != length * 2) {
            write_str_file(ack_path, "err:hex_length_mismatch");
            unlink(req_path);
            continue;
        }
        for (int i = 0; i < length; i++) {
            unsigned int byte_val;
            sscanf(data_hex + i * 2, "%02x", &byte_val);
            write_buf[i] = (uint8_t)byte_val;
        }

        /* Perform I2C write via hidraw */
        int bus = sfp_bus_index[port];
        int mux_addr, mux_chan;
        bus_to_mux(bus, &mux_addr, &mux_chan);
        int rc = cp2112_write_eeprom(mux_addr, mux_chan, offset, write_buf, length);

        if (rc < 0) {
            write_str_file(ack_path, "err:i2c_write_failed");
        } else {
            /* Refresh EEPROM cache */
            refresh_eeprom_lower_page(port, eeprom_path);
            write_str_file(ack_path, "ok");
        }
        unlink(req_path);
    }
}
```

  Note: `cp2112_write_eeprom()` must be implemented using the existing CP2112 hidraw infrastructure. Use `set_lpmode_hidraw()` as the pattern for CP2112 I2C write. Add a helper `cp2112_write_eeprom(mux_addr, mux_chan, offset, data, len)` that:
  - Selects the mux channel: `cp2112_i2c_write_no_stop(mux_addr, &mux_chan_byte, 1)`
  - Writes the EEPROM data: `cp2112_i2c_write(0x50, reg_buf, len+1)` where reg_buf[0]=offset
  - Deselects mux: `cp2112_i2c_write(mux_addr, &zero, 1)`

- [ ] Step 6: Add read request handling (`poll_read_requests_hidraw()`) similarly — for each `sfp_N_read_req` file, read the EEPROM lower page and write the hex result to `sfp_N_read_resp`.

- [ ] Step 7: Build the daemon to verify compilation:
  ```bash
  cd platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils
  gcc -O2 -o wedge100s-i2c-daemon-test wedge100s-i2c-daemon.c 2>&1 | head -20
  ```
  Expected: compiles without errors.

- [ ] Step 8: Build and deploy the full .deb:
  ```bash
  BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
  scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
  ssh admin@192.168.88.12 'sudo systemctl stop pmon && sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb && sudo systemctl start pmon'
  ```

- [ ] Step 9: Verify write path (LP_MODE assert): `cd tests && pytest stage_21_lpmode -v`
  Expected: all tests pass (LP_MODE uses the existing request/response file protocol — unchanged — but this confirms the daemon still processes request files correctly alongside the new write_req handling).

- [ ] Step 10: Verify DOM read path: `cd tests && pytest stage_11_transceiver -v`
  Expected: all tests pass (DOM data still returned for present ports).

- [ ] Step 11: Remove smbus2 imports from sfp.py if all paths migrated:
  - Remove `from smbus2 import SMBus, i2c_msg as _i2c_msg`
  - Remove `_SMBUS2_OK` check
  - Remove `_eeprom_bus_lock` (no longer needed)
  - Remove `_hardware_read_eeprom()` (no longer called after write migration)
  - Remove `_PCA9535_BUS`, `_PCA9535_ADDR` constants (fallback presence path still uses smbus2 — keep if presence fallback is still needed)

- [ ] Step 12: Run full test suite to confirm no regressions: `cd tests && python3 run_tests.py -- -x`

- [ ] Step 13: Commit.

---

### Task 8: Section 4a/4d Implementation (Investigation-Gated)

**This task is conditional on Task 5 investigation results.**

- **4a (INT_L persistent daemon)**: Implement ONLY if Task 5 confirmed a host CPU GPIO wired to PCA9535 INT.
- **4d (USB-CDC-Ethernet)**: Implement ONLY if Task 5 confirmed CDC-ECM availability.

If neither investigation confirmed feasibility, this task is CANCELLED. Document in `tests/notes/` and close.

**If 4a confirmed:**

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-i2c-daemon.c`
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/service/wedge100s-i2c-poller.timer` → replace with `wedge100s-i2c-poller.service` (Type=simple)

- [ ] Step 1 (4a): Convert daemon from one-shot to persistent (main loop with `poll()` on GPIO fd).
- [ ] Step 2 (4a): Replace `wedge100s-i2c-poller.timer` + `wedge100s-i2c-daemon.service` with a single `Type=simple` persistent service.
- [ ] Step 3 (4a): Add 10s watchdog timer using `timerfd_create()` for fallback polling.
- [ ] Step 4 (4a): Build, deploy, run full test suite.

**If 4d confirmed:**

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/bmc.py` (if exists) or wherever TTY commands are sent

- [ ] Step 1 (4d): Assign private IPs on the USB-CDC-Ethernet interface.
- [ ] Step 2 (4d): Replace TTY session with TCP command socket or SSH subprocess.
- [ ] Step 3 (4d): Verify BMC responses (uptime, fan speed, PSU status) match TTY path.
- [ ] Step 4 (4d): Run `cd tests && python3 run_tests.py stage_03_platform stage_04_thermal stage_05_fan stage_06_psu`.
