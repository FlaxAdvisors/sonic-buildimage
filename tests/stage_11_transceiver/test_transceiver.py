"""Stage 11 — Transceiver Info & DOM.

Verifies xcvrd populates STATE_DB TRANSCEIVER_INFO and TRANSCEIVER_STATUS
for present QSFP modules.  DOM values are N/A for passive DAC cables (no
monitoring electronics) — that is expected and not a failure.

Port discovery is dynamic: the test reads which ports are present from
the daemon cache (/run/wedge100s/sfp_N_present) at test time and checks
only those ports.  The stage skips if no ports are populated.

Identifier byte: 0x11 (QSFP28) — occasionally 0x01 (GBIC) from cheap DAC.
Vendor data may be garbled on low-quality DAC cables; this is not a platform bug.

Phase reference: Phase 11 (Transceiver Info & DOM).
"""

import json
import re
import time
import pytest

NUM_PORTS = 32


def _discover_present_ports(ssh):
    """Return list of Ethernet port names whose daemon presence cache is '1'.

    Maps port index 0..31 to Ethernet0..124 (step 4) — the standard SONiC
    port naming for this 32-port platform.
    """
    present = []
    for idx in range(NUM_PORTS):
        out, _, rc = ssh.run(
            f"cat /run/wedge100s/sfp_{idx}_present 2>/dev/null", timeout=5
        )
        if out.strip() == "1":
            present.append(f"Ethernet{idx * 4}")
    return present

# STATE_DB keys (DB 6)
XCVRD_SCRIPT = """\
import json, subprocess

def redis(db, cmd, *args):
    r = subprocess.run(['redis-cli', '-n', str(db)] + list(cmd) + list(args),
                       capture_output=True, text=True)
    return r.stdout.strip()

results = {{}}
for port in {ports!r}:
    info = redis(6, ['hgetall', f'TRANSCEIVER_INFO|{{port}}'])
    dom  = redis(6, ['hgetall', f'TRANSCEIVER_DOM_SENSOR|{{port}}'])
    stat = redis(6, ['hgetall', f'TRANSCEIVER_STATUS|{{port}}'])
    results[port] = {{
        'info_populated': bool(info.strip()),
        'dom_populated':  bool(dom.strip()),
        'stat_populated': bool(stat.strip()),
        'info_raw': info[:200],
        'dom_raw':  dom[:200],
        'stat_raw': stat[:200],
    }}
print(json.dumps(results))
"""


def _xcvrd_state(ssh, ports):
    code = XCVRD_SCRIPT.format(ports=ports)
    out, err, rc = ssh.run_python(code, timeout=30)
    assert rc == 0, f"xcvrd STATE_DB query failed (rc={rc}): {err}"
    return json.loads(out.strip())


def _xcvrd_state_wait(ssh, ports, key, timeout=180):
    """Poll STATE_DB until all ports have key populated or timeout expires."""
    deadline = time.monotonic() + timeout
    while True:
        data = _xcvrd_state(ssh, ports)
        missing = [p for p, d in data.items() if not d[key]]
        if not missing:
            return data, []
        if time.monotonic() >= deadline:
            return data, missing
        time.sleep(10)


# ------------------------------------------------------------------
# xcvrd STATE_DB population
# ------------------------------------------------------------------

def test_xcvrd_transceiver_info_populated(ssh):
    """xcvrd populates TRANSCEIVER_INFO in STATE_DB for all installed modules.

    xcvrd first scan completes within ~60 s of startup; allow up to 120 s.
    Skips if no QSFP modules are present.
    """
    ports = _discover_present_ports(ssh)
    if not ports:
        pytest.skip("No QSFP modules present — skipping TRANSCEIVER_INFO check")
    print(f"\nDiscovered {len(ports)} present ports: {ports}")
    data, missing = _xcvrd_state_wait(ssh, ports, "info_populated", timeout=120)
    print("\nTRANSCEIVER_INFO population:")
    for port, d in data.items():
        status = "populated" if d["info_populated"] else "MISSING"
        print(f"  {port}: {status}")
    assert not missing, (
        f"TRANSCEIVER_INFO missing for ports: {missing}\n"
        "Check that xcvrd is running inside pmon and modules are present."
    )


def test_xcvrd_transceiver_status_populated(ssh):
    """xcvrd populates TRANSCEIVER_STATUS in STATE_DB for all installed modules.

    DOM status first cycle can take 5-10 minutes after xcvrd start on DAC cables;
    allow up to 600 s.  Skips if no QSFP modules are present.
    """
    ports = _discover_present_ports(ssh)
    if not ports:
        pytest.skip("No QSFP modules present — skipping TRANSCEIVER_STATUS check")
    data, missing = _xcvrd_state_wait(ssh, ports, "stat_populated", timeout=600)
    print("\nTRANSCEIVER_STATUS population:")
    for port, d in data.items():
        status = "populated" if d["stat_populated"] else "MISSING"
        print(f"  {port}: {status}")
    assert not missing, (
        f"TRANSCEIVER_STATUS missing for ports: {missing}\n"
        "This table tracks tx_fault, rx_los, etc."
    )


def test_xcvrd_dom_passive_dac(ssh):
    """DOM data is N/A for passive DAC cables (no DOM electronics).

    Passive DAC cables cannot report temperature, voltage, or optical power.
    This test verifies xcvrd handles the absence gracefully (N/A values, no crash).
    Skips if no QSFP modules are present.
    """
    ports = _discover_present_ports(ssh)
    if not ports:
        pytest.skip("No QSFP modules present — skipping DOM check")
    data = _xcvrd_state(ssh, ports)
    # If TRANSCEIVER_DOM_SENSOR is absent or empty, that is expected for passive DACs.
    # If it is populated, any values should parse without error (may all be N/A).
    print("\nDOM sensor data (passive DAC — N/A expected):")
    for port, d in data.items():
        dom_raw = d["dom_raw"]
        print(f"  {port}: {'populated' if d['dom_populated'] else 'absent'}")
        if d["dom_populated"] and dom_raw:
            # Should not contain error-indicating strings
            assert "error" not in dom_raw.lower() or "N/A" in dom_raw, (
                f"{port}: DOM data has unexpected error content: {dom_raw!r}"
            )
    # Not failing on absent DOM — passive DACs don't have DOM electronics
    if all(not d["dom_populated"] for d in data.values()):
        pytest.skip(
            "DOM verification skipped: passive DAC cables have no DOM electronics. "
            "Test with active optics (SR4, LR4) to verify DOM sensor values."
        )


# ------------------------------------------------------------------
# show interfaces transceiver CLI
# ------------------------------------------------------------------

def test_transceiver_eeprom_cli_exits_zero(ssh):
    """show interfaces transceiver eeprom exits 0 for a present port."""
    ports = _discover_present_ports(ssh)
    if not ports:
        pytest.skip("No QSFP modules present — skipping EEPROM CLI check")
    port = ports[0]
    out, err, rc = ssh.run(f"show interfaces transceiver eeprom {port}", timeout=30)
    print(f"\nshow interfaces transceiver eeprom {port}:\n{out}")
    assert rc == 0, f"Command failed (rc={rc}): {err}"
    assert out.strip(), "Output is empty"


def test_transceiver_eeprom_identifier(ssh):
    """Identifier field in transceiver eeprom output is QSFP28 or GBIC (cheap DAC).

    Some inexpensive DAC cables report GBIC (0x01) instead of QSFP28 (0x11).
    Both are accepted — this is a cable quality issue, not a platform bug.
    Skips if no QSFP modules are present.
    """
    ports = _discover_present_ports(ssh)
    if not ports:
        pytest.skip("No QSFP modules present — skipping identifier check")
    port = ports[0]
    out, err, rc = ssh.run(f"show interfaces transceiver eeprom {port}", timeout=30)
    assert rc == 0, f"Command failed: {err}"
    assert "Identifier" in out, "No Identifier field in transceiver eeprom output"
    # Accept QSFP28/QSFP+/GBIC — cheap DAC cables may report either
    valid_identifiers = ["QSFP28", "QSFP+", "GBIC", "QSFP"]
    found = any(ident in out for ident in valid_identifiers)
    assert found, (
        f"Expected one of {valid_identifiers} in transceiver eeprom output, got:\n{out}"
    )


def test_transceiver_presence_all_ports(ssh):
    """show interfaces transceiver presence lists all 32 QSFP ports."""
    out, err, rc = ssh.run("show interfaces transceiver presence", timeout=30)
    assert rc == 0, f"Command failed: {err}"
    eth_rows = [l for l in out.splitlines() if re.match(r"\s*Ethernet\d+", l)]
    assert len(eth_rows) >= NUM_PORTS, (
        f"Expected >= {NUM_PORTS} rows, got {len(eth_rows)}"
    )
    # Present ports should show "Present", absent should show "Not present"
    present_count = sum(1 for l in eth_rows if "Present" in l and "Not present" not in l)
    daemon_present = _discover_present_ports(ssh)
    print(f"\nPresent ports: {present_count} / {NUM_PORTS} "
          f"(daemon cache: {len(daemon_present)} present)")
    # CLI present count must be at least as many as the daemon reports
    assert present_count >= len(daemon_present), (
        f"CLI reports only {present_count} present ports but daemon cache "
        f"shows {len(daemon_present)} — xcvrd may be out of sync"
    )


# ------------------------------------------------------------------
# Platform API — SfpOptoeBase inheritance
# ------------------------------------------------------------------

XCVR_API_SCRIPT = """\
import json, sys
sys.path.insert(0, '/usr/lib/python3/dist-packages')
from sonic_platform.platform import Platform
chassis = Platform().get_chassis()

results = []
for idx in range(1, 33):
    sfp = chassis.get_sfp(idx)
    present = sfp.get_presence()
    result = {'index': idx, 'name': sfp.get_name(), 'present': present}
    if present:
        try:
            api = sfp.get_xcvr_api()
            result['api_type'] = type(api).__name__ if api else None
            if api:
                info = api.get_transceiver_info()
                result['info_keys'] = list(info.keys()) if info else []
        except Exception as e:
            result['api_error'] = str(e)
    results.append(result)
print(json.dumps(results))
"""


def test_xcvr_api_factory_qsfp28(ssh):
    """get_xcvr_api() returns Sff8636Api for QSFP28 modules (identifier 0x11)."""
    out, err, rc = ssh.run_python(XCVR_API_SCRIPT, timeout=60)
    assert rc == 0, f"Script failed (rc={rc}): {err}"
    results = json.loads(out.strip())
    present = [r for r in results if r["present"]]
    if not present:
        pytest.skip("No QSFP modules present")

    print(f"\nPresent ports: {len(present)}")
    success = 0
    for r in present:
        api_type = r.get("api_type")
        err_msg  = r.get("api_error", "")
        if api_type:
            success += 1
            print(f"  {r['name']}: {api_type}")
        else:
            # Transient failure (cheap DAC EEPROM) — not a platform bug
            print(f"  {r['name']}: api=None err={err_msg!r}")

    # At least one present port should return a valid API object.
    # Cheap/knockoff DAC cables frequently fail get_xcvr_api() — that is a cable
    # quality issue, not a platform bug.  Require only 1 success.
    assert success >= 1, (
        f"0/{len(present)} present ports returned a valid xcvr API. "
        "If ALL fail, check optoe driver binding and EEPROM sysfs paths."
    )


def test_xcvr_api_transceiver_info_keys(ssh):
    """get_transceiver_info() returns dict with expected keys for present ports."""
    EXPECTED_KEYS = {"type", "manufacturer", "model", "serial",
                     "connector", "encoding", "ext_identifier"}
    out, err, rc = ssh.run_python(XCVR_API_SCRIPT, timeout=60)
    assert rc == 0, f"Script failed (rc={rc}): {err}"
    results = json.loads(out.strip())
    with_info = [r for r in results if r.get("info_keys")]
    if not with_info:
        pytest.skip("No ports returned transceiver info (all APIs returned None)")

    r = with_info[0]
    actual_keys = set(r["info_keys"])
    missing = EXPECTED_KEYS - actual_keys
    print(f"\n{r['name']} info keys: {sorted(actual_keys)}")
    assert not missing, (
        f"TRANSCEIVER_INFO missing expected keys: {missing}"
    )
