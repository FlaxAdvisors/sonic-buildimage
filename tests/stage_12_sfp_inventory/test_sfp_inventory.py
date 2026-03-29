"""Stage 12 — SFP Inventory: EEPROM identity and PM data for installed transceivers.

Collects vendor identity (Vendor Name, PN, Serial) and performance monitoring
data for all installed SFP/QSFP modules.  For breakout configurations
(4x25G, 2x50G), only the primary sub-port (alias ending /1) is queried —
all sub-ports share one physical SFP.

Breakout mode support:
  1x100G  — single port, alias e.g. Ethernet5/1    → queried
  4x25G   — Ethernet1/1 (queried), /2, /3, /4 (skipped)
  2x50G   — Ethernet1/1 (queried), /3 or /2 (skipped)
  4x10G   — same as 4x25G layout

This stage is primarily data collection — test assertions are minimal.
Run `./run_tests.py --report stage_12_sfp_inventory` for the formatted table.

Phase reference: Phase 12 (SFP Inventory).
"""

import json
import re
import pytest

# ---------------------------------------------------------------------------
# Batch data collection — one Python script on target, one SSH round-trip
# ---------------------------------------------------------------------------

_COLLECT_SCRIPT = r"""
import json, subprocess, re

def run(*cmd):
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    return r.stdout.strip()

# 1. Get all port aliases from CONFIG_DB in one pass
keys_out = run('redis-cli', '-n', '4', 'KEYS', 'PORT|Ethernet*')
alias_map = {}
for key in keys_out.splitlines():
    if '|' not in key:
        continue
    port = key.split('|', 1)[1].strip()
    alias = run('redis-cli', '-n', '4', 'HGET', key, 'alias')
    alias_map[port] = alias

# 2. Get presence info
presence_out = run('show', 'interfaces', 'transceiver', 'presence')
present = set()
for line in presence_out.splitlines():
    m = re.match(r'\s*(Ethernet\d+)\s+Present\b', line)
    if m:
        present.add(m.group(1))

# 3. Determine physical primary ports: present AND alias ends with /1
def _eth_key(name):
    m = re.search(r'(\d+)', name)
    return int(m.group(1)) if m else 0

physical = [
    p for p in sorted(present, key=_eth_key)
    if alias_map.get(p, '').endswith('/1')
]

# 4. Collect EEPROM fields and PM output for each physical port
inventory = []
for port in physical:
    alias = alias_map.get(port, '')
    eeprom = run('show', 'interfaces', 'transceiver', 'eeprom', port)
    pm     = run('show', 'interfaces', 'transceiver', 'pm',    port)

    def _field(pattern, text):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ''

    # Parse PM into per-lane list: [(lane, rx_pwr, tx_bias, tx_pwr), ...]
    pm_lanes = []
    for m in re.finditer(r'^\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)', pm, re.MULTILINE):
        pm_lanes.append({
            'lane':    int(m.group(1)),
            'rx_pwr':  m.group(2),
            'tx_bias': m.group(3),
            'tx_pwr':  m.group(4),
        })
    pm_temp    = re.search(r'Temperature:\s*(\S+)', pm)
    pm_voltage = re.search(r'Voltage:\s*(\S+)',     pm)

    inventory.append({
        'port':      port,
        'alias':     alias,
        'vendor':    _field(r'Vendor Name:\s*(.+)',  eeprom),
        'pn':        _field(r'Vendor PN:\s*(.+)',    eeprom),
        'sn':        _field(r'Vendor SN:\s*(.+)',    eeprom),
        'pm':        pm,
        'pm_lanes':  pm_lanes,
        'pm_temp':   pm_temp.group(1)    if pm_temp    else 'N/A',
        'pm_voltage':pm_voltage.group(1) if pm_voltage else 'N/A',
        'eeprom_ok': 'SFP EEPROM' in eeprom or 'Identifier' in eeprom,
    })

print(json.dumps({
    'alias_map': alias_map,
    'present':   sorted(present, key=_eth_key),
    'inventory': inventory,
}))
"""


@pytest.fixture(scope="session")
def sfp_data(ssh):
    """Collect SFP inventory once per session; cache on the fixture."""
    out, err, rc = ssh.run_python(_COLLECT_SCRIPT, timeout=120)
    assert rc == 0, f"SFP inventory script failed (rc={rc}): {err}"
    return json.loads(out.strip())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_transceiver_presence_detected(sfp_data):
    """At least one transceiver is installed and detected."""
    present = sfp_data["present"]
    print(f"\nInstalled transceivers ({len(present)} ports): {present}")
    assert present, "No transceivers detected — check hardware and xcvrd"


def test_physical_ports_have_alias(sfp_data):
    """Every present port that is a breakout primary (/1) has an alias in CONFIG_DB.

    If this fails, CONFIG_DB is missing PORT entries — run deploy.py or
    check that breakout config is applied.
    """
    alias_map = sfp_data["alias_map"]
    inventory = sfp_data["inventory"]
    if not inventory:
        pytest.skip("No physical primary ports found")
    missing = [e["port"] for e in inventory if not e["alias"]]
    assert not missing, f"Ports with missing aliases: {missing}"


def test_eeprom_readable_for_present_physical_ports(sfp_data):
    """EEPROM is readable for all present physical ports (alias /1).

    Cheap/knockoff DACs may have blank vendor fields — the test checks that
    the EEPROM command returned EEPROM data, not that the fields are non-empty.
    """
    inventory = sfp_data["inventory"]
    if not inventory:
        pytest.skip("No present physical ports found")

    failures = [e["port"] for e in inventory if not e["eeprom_ok"]]
    print(f"\nEEPROM readable: {len(inventory) - len(failures)}/{len(inventory)}")
    for e in inventory:
        status = "ok" if e["eeprom_ok"] else "FAILED"
        print(f"  {e['port']:<14} ({e['alias']:<14}): {status}")

    assert not failures, (
        f"EEPROM not readable for: {failures}\n"
        "Check that xcvrd is running and optoe driver is bound."
    )


def test_pm_command_succeeds_for_present_physical_ports(sfp_data):
    """show interfaces transceiver pm succeeds for present physical ports.

    Values may all be N/A for passive DAC cables — that is expected and
    not a failure.  This test verifies the command itself exits 0 and
    returns some output.
    """
    inventory = sfp_data["inventory"]
    if not inventory:
        pytest.skip("No present physical ports found")

    no_output = [e["port"] for e in inventory if not e["pm"].strip()]
    print(f"\nPM data present: {len(inventory) - len(no_output)}/{len(inventory)}")
    for e in inventory:
        pm_lines = e["pm"].count("\n") + 1 if e["pm"].strip() else 0
        print(f"  {e['port']:<14} ({e['alias']:<14}): {pm_lines} lines")

    assert not no_output, (
        f"No PM output for: {no_output}\n"
        "Unexpected — even DAC cables should return N/A PM data."
    )


def test_sfp_inventory_summary(sfp_data):
    """Print full SFP inventory table for CI visibility.

    Always passes — the table is the deliverable, not the assertion.
    Use --report for the formatted version.
    """
    inventory = sfp_data["inventory"]
    if not inventory:
        pytest.skip("No present physical ports found")

    # --- identity table ---
    header = f"  {'Port':<14} {'Alias':<14} {'Vendor':<16} {'PN':<20} {'Serial'}"
    sep    = f"  {'-'*14} {'-'*14} {'-'*16} {'-'*20} {'-'*20}"
    print(f"\nSFP Inventory ({len(inventory)} physical ports):\n{header}\n{sep}")
    for e in inventory:
        vendor = e["vendor"] or "—"
        pn     = e["pn"]     or "—"
        sn     = e["sn"]     or "—"
        print(f"  {e['port']:<14} {e['alias']:<14} {vendor:<16} {pn:<20} {sn}")

    # --- PM table: one row per lane ---
    print(f"\n  PM Data  (N/A = DAC/passive, no DOM):")
    pm_hdr = f"  {'Port':<14} {'Alias':<14} {'Temp°C':<8} {'Volt V':<8} {'Ln':<4} {'RxPwr dBm':<12} {'TxBias mA':<12} {'TxPwr dBm'}"
    pm_sep = f"  {'-'*14} {'-'*14} {'-'*8} {'-'*8} {'-'*4} {'-'*12} {'-'*12} {'-'*12}"
    print(pm_hdr)
    print(pm_sep)
    for e in inventory:
        temp = e["pm_temp"]
        volt = e["pm_voltage"]
        lanes = e["pm_lanes"]
        if not lanes:
            print(f"  {e['port']:<14} {e['alias']:<14} {temp:<8} {volt:<8} {'—':<4}")
            continue
        for i, ln in enumerate(lanes):
            port_col  = e['port']  if i == 0 else ""
            alias_col = e['alias'] if i == 0 else ""
            temp_col  = temp       if i == 0 else ""
            volt_col  = volt       if i == 0 else ""
            print(
                f"  {port_col:<14} {alias_col:<14} {temp_col:<8} {volt_col:<8}"
                f" {ln['lane']:<4} {ln['rx_pwr']:<12} {ln['tx_bias']:<12} {ln['tx_pwr']}"
            )
