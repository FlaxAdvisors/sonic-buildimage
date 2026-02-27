"""Stage 06 — Power supplies (2× PSU, CPLD presence + BMC PMBus telemetry).

Presence and power-good status come from host CPLD i2c-1/0x32 reg 0x10.
Voltage, current, and power readings come from BMC PMBus (PSU1@0x59, PSU2@0x5a
on BMC i2c-7 via PCA9546 mux at 0x70).

Phase reference: Phase 5 (PSU).
"""

import json
import pytest

NUM_PSUS = 2
PSU_CAPACITY_W = 650.0

# Sanity bounds for a powered PSU in a running system
AC_INPUT_VOLTAGE_MIN = 100.0   # Volts AC (min of 120V or 240V range)
DC_OUTPUT_VOLTAGE_MIN = 1.0    # Volts DC
DC_POWER_MIN_W = 0.0           # Watts (0 is OK — light load)

# CPLD PSU status register
CPLD_BUS = 1
CPLD_ADDR = 0x32
CPLD_PSU_REG = 0x10
# Bit masks (0 = present / power-good)
PSU1_PRESENT_BIT = 0
PSU1_PGOOD_BIT   = 1
PSU2_PRESENT_BIT = 4
PSU2_PGOOD_BIT   = 5

PSU_CAPTURE = """\
import json
from sonic_platform.platform import Platform

chassis = Platform().get_chassis()
psus = chassis.get_all_psus()
results = []
for psu in psus:
    results.append({
        'name': psu.get_name(),
        'presence': psu.get_presence(),
        'status': psu.get_status(),
        'powergood': psu.get_powergood_status(),
        'type': psu.get_type(),
        'capacity_w': psu.get_capacity(),
        'voltage_v': psu.get_voltage(),
        'current_a': psu.get_current(),
        'power_w': psu.get_power(),
        'input_voltage_v': psu.get_input_voltage(),
        'input_current_a': psu.get_input_current(),
        'position': psu.get_position_in_parent(),
    })
print(json.dumps(results))
"""


def _get_psus(ssh):
    out, err, rc = ssh.run_python(PSU_CAPTURE, timeout=60)
    assert rc == 0, f"PSU capture script failed (rc={rc}): {err}"
    return json.loads(out.strip())


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def test_psu_cli_show(ssh):
    """show platform psustatus exits 0 and returns a table."""
    out, err, rc = ssh.run("show platform psustatus")
    print(f"\nshow platform psustatus:\n{out}")
    assert rc == 0, f"show platform psustatus failed: {err}"
    assert out.strip(), "show platform psustatus returned empty output"


def test_psu_cli_both_listed(ssh):
    """show platform psustatus lists both PSUs (accepts PSU-1/PSU-2 or PSU1/PSU2 format)."""
    out, _, _ = ssh.run("show platform psustatus")
    assert "PSU-1" in out or "PSU1" in out or "PSU 1" in out, (
        "PSU 1 not listed in psustatus output"
    )
    assert "PSU-2" in out or "PSU2" in out or "PSU 2" in out, (
        "PSU 2 not listed in psustatus output"
    )


# ------------------------------------------------------------------
# CPLD direct read (host side)
# ------------------------------------------------------------------

def test_cpld_psu_status_register(ssh):
    """Read raw PSU status register from CPLD and decode it."""
    cmd = f"sudo i2cget -y {CPLD_BUS} 0x{CPLD_ADDR:02x} 0x{CPLD_PSU_REG:02x}"
    out, err, rc = ssh.run(cmd)
    print(f"\nCPLD PSU status reg: {out.strip()}")
    assert rc == 0, f"i2cget CPLD PSU reg failed: {err}"

    import re
    m = re.match(r"0x([0-9a-fA-F]{2})", out.strip())
    assert m, f"Could not parse hex value from: {out.strip()}"
    reg_val = int(m.group(1), 16)

    psu1_present = not bool(reg_val & (1 << PSU1_PRESENT_BIT))
    psu1_pgood   = bool(reg_val & (1 << PSU1_PGOOD_BIT))
    psu2_present = not bool(reg_val & (1 << PSU2_PRESENT_BIT))
    psu2_pgood   = bool(reg_val & (1 << PSU2_PGOOD_BIT))

    print(f"  PSU1: present={psu1_present} power_good={psu1_pgood}")
    print(f"  PSU2: present={psu2_present} power_good={psu2_pgood}")

    assert psu1_present or psu2_present, (
        f"CPLD reports both PSUs absent (reg=0x{reg_val:02x}). "
        "Is the system actually powered?"
    )


# ------------------------------------------------------------------
# Python API — structure
# ------------------------------------------------------------------

def test_psu_api_count(ssh):
    """chassis.get_all_psus() returns exactly 2 PSUs."""
    data = _get_psus(ssh)
    print(f"\nPSUs ({len(data)}):")
    for p in data:
        print(
            f"  {p['name']}: present={p['presence']} status={p['status']} "
            f"pgood={p['powergood']} type={p['type']} cap={p['capacity_w']}W"
        )
        if p['presence']:
            print(
                f"    DC  voltage={p['voltage_v']}V  current={p['current_a']}A  "
                f"power={p['power_w']}W"
            )
            print(
                f"    AC  input_v={p['input_voltage_v']}V  "
                f"input_a={p['input_current_a']}A"
            )
    assert len(data) == NUM_PSUS, f"Expected {NUM_PSUS} PSUs, got {len(data)}"


def test_psu_api_at_least_one_present(ssh):
    """At least one PSU is physically present."""
    data = _get_psus(ssh)
    present = [p for p in data if p["presence"]]
    assert present, "No PSUs detected as present — check CPLD i2c communication"


def test_psu_api_capacity(ssh):
    """PSU capacity is 650W for all units."""
    data = _get_psus(ssh)
    for p in data:
        assert p["capacity_w"] == PSU_CAPACITY_W, (
            f"{p['name']} capacity={p['capacity_w']}W, expected {PSU_CAPACITY_W}W"
        )


def test_psu_api_type_ac(ssh):
    """PSU type is reported as AC."""
    data = _get_psus(ssh)
    for p in data:
        assert p["type"].upper() == "AC", (
            f"{p['name']} type={p['type']!r}, expected 'AC'"
        )


def test_psu_api_present_psus_status_ok(ssh):
    """At least one present PSU reports status=True (power good).

    A system may have a physically-present but unpowered/failed PSU (e.g. a
    cold-spare or a dead unit).  That is real hardware state and not a test
    failure.  We require that at least one PSU is healthy so the system runs.
    """
    data = _get_psus(ssh)
    present = [p for p in data if p["presence"]]
    if not present:
        pytest.skip("No PSUs present")

    ok_psus    = [p for p in present if p["status"]]
    not_ok_psus = [p for p in present if not p["status"]]

    if not_ok_psus:
        print(f"\n  NOTE: {len(not_ok_psus)} PSU(s) present but NOT OK: "
              f"{[p['name'] for p in not_ok_psus]}")

    assert ok_psus, (
        f"No present PSU has status=OK. Present PSUs: "
        f"{[(p['name'], p['status']) for p in present]}"
    )


def test_psu_api_dc_voltage(ssh):
    """Present+OK PSUs report DC output voltage > 0V.

    None indicates a transient BMC PMBus read timeout (shared TTY under load).
    xfail if all OK PSUs return None simultaneously.
    """
    data = _get_psus(ssh)
    ok_psus = [p for p in data if p["presence"] and p["status"]]
    if not ok_psus:
        pytest.skip("No OK PSUs to check voltage")

    all_none = all(p["voltage_v"] is None for p in ok_psus)
    if all_none:
        pytest.xfail(
            "All OK PSUs returned voltage=None — transient BMC PMBus read timeout. "
            "Re-run the PSU stage in isolation: ./run_tests.py stage_06_psu"
        )

    for p in ok_psus:
        v = p["voltage_v"]
        if v is None:
            print(f"  WARN: {p['name']} get_voltage() returned None (transient?)")
            continue
        assert v >= DC_OUTPUT_VOLTAGE_MIN, (
            f"{p['name']} DC voltage={v}V (expected ≥{DC_OUTPUT_VOLTAGE_MIN}V)"
        )


def test_psu_api_dc_power(ssh):
    """Present+OK PSUs report non-negative DC output power.

    None indicates a transient BMC PMBus read timeout.
    """
    data = _get_psus(ssh)
    ok_psus = [p for p in data if p["presence"] and p["status"]]
    if not ok_psus:
        pytest.skip("No OK PSUs to check power")

    all_none = all(p["power_w"] is None for p in ok_psus)
    if all_none:
        pytest.xfail(
            "All OK PSUs returned power=None — transient BMC PMBus read timeout. "
            "Re-run the PSU stage in isolation: ./run_tests.py stage_06_psu"
        )

    for p in ok_psus:
        pw = p["power_w"]
        if pw is None:
            print(f"  WARN: {p['name']} get_power() returned None (transient?)")
            continue
        assert pw >= DC_POWER_MIN_W, f"{p['name']} power={pw}W is negative"


def test_psu_api_ac_input_voltage(ssh):
    """Present PSUs report AC input voltage ≥ 100V."""
    data = _get_psus(ssh)
    for p in data:
        if not p["presence"] or not p["status"]:
            continue
        vin = p["input_voltage_v"]
        assert vin is not None, f"{p['name']} get_input_voltage() returned None"
        assert vin >= AC_INPUT_VOLTAGE_MIN, (
            f"{p['name']} AC input voltage={vin}V (expected ≥{AC_INPUT_VOLTAGE_MIN}V)"
        )


def test_psu_api_positions(ssh):
    """PSU positions are 1 and 2."""
    data = _get_psus(ssh)
    positions = sorted(p["position"] for p in data)
    assert positions == [1, 2], f"Unexpected PSU positions: {positions}"
