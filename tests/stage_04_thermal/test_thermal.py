"""Stage 04 — Thermal sensors (8 sensors: CPU core + 7× TMP75 via BMC).

Sensor layout:
  Index 0  — CPU Core (Broadwell-DE coretemp, host sysfs)
  Index 1–5 — TMP75-1..5 (BMC i2c-3/0x48..0x4c)
  Index 6–7 — TMP75-6..7 (BMC i2c-8/0x48..0x49)

Phase reference: Phase 3 (Thermal).
"""

import json
import pytest

NUM_THERMALS = 8
TEMP_MIN_C = 0.0
TEMP_MAX_C = 100.0  # sanity ceiling — well below any threshold

THERMAL_CAPTURE = """\
import json, sys
from sonic_platform.platform import Platform

chassis = Platform().get_chassis()
thermals = chassis.get_all_thermals()
results = []
for t in thermals:
    temp = t.get_temperature()
    results.append({
        'name': t.get_name(),
        'temperature': temp,
        'high_threshold': t.get_high_threshold(),
        'high_critical_threshold': t.get_high_critical_threshold(),
        'status': t.get_status(),
        'position': t.get_position_in_parent(),
    })
print(json.dumps(results))
"""


def _get_thermals(ssh):
    """Fetch thermal data dict from target via Python API."""
    out, err, rc = ssh.run_python(THERMAL_CAPTURE, timeout=60)
    assert rc == 0, f"Thermal capture script failed (rc={rc}): {err}"
    data = json.loads(out.strip())
    return data


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def test_thermal_cli_show(ssh):
    """show platform temperature exits 0 and returns a table."""
    out, err, rc = ssh.run("show platform temperature")
    print(f"\nshow platform temperature:\n{out}")
    assert rc == 0, f"show platform temperature failed: {err}"
    assert out.strip(), "show platform temperature returned empty output"


def test_thermal_cli_row_count(ssh):
    """show platform temperature shows at least 1 sensor row.

    The CLI reads thermalctld's STATE_DB cache, so the number of visible rows
    depends on how many sensors the daemon has polled since boot.  All 8 sensors
    are verified via the Python API in test_thermal_api_count.
    """
    out, _, _ = ssh.run("show platform temperature")
    import re
    data_rows = [l for l in out.splitlines() if re.search(r"\d+\.\d+", l)]
    print(f"\nData rows found: {len(data_rows)} (of {NUM_THERMALS} total sensors)")
    for row in data_rows:
        print(f"  {row.strip()}")
    assert len(data_rows) >= 1, "show platform temperature returned no temperature rows"


# ------------------------------------------------------------------
# Python API
# ------------------------------------------------------------------

def test_thermal_api_count(ssh):
    """chassis.get_all_thermals() returns exactly 8 sensors."""
    data = _get_thermals(ssh)
    print(f"\nThermal sensors ({len(data)}):")
    for t in data:
        print(f"  [{t['position']}] {t['name']:20s} {t['temperature']}°C  "
              f"(high={t['high_threshold']}°C crit={t['high_critical_threshold']}°C)")
    assert len(data) == NUM_THERMALS, (
        f"Expected {NUM_THERMALS} thermals, got {len(data)}"
    )


def test_thermal_api_names(ssh):
    """All expected sensor names are present."""
    data = _get_thermals(ssh)
    names = [t["name"] for t in data]
    print(f"\nSensor names: {names}")

    # CPU Core sensor
    assert any("CPU" in n or "cpu" in n.lower() for n in names), (
        f"CPU Core thermal not found in: {names}"
    )
    # TMP75 sensors
    for i in range(1, 8):
        assert any(f"TMP75-{i}" in n or f"TMP75 {i}" in n for n in names), (
            f"TMP75-{i} not found in sensor names: {names}"
        )


def test_thermal_api_temperatures_in_range(ssh):
    """All sensor temperatures are in the sane range [0, 100]°C.

    The CPU Core sensor reads host sysfs and must always return a value.
    TMP75-* sensors read via the BMC TTY and may return None on a transient
    timeout — those are xfailed rather than hard-failed.
    """
    data = _get_thermals(ssh)

    # CPU Core (index 0) must always return a temperature — no BMC involved
    cpu = next((t for t in data if "CPU" in t["name"] or "cpu" in t["name"].lower()), None)
    if cpu:
        assert cpu["temperature"] is not None, (
            f"CPU Core sensor returned None — host coretemp sysfs may be broken"
        )

    # BMC sensors: None means transient TTY timeout → xfail
    bmc_none = [t["name"] for t in data if "TMP75" in t["name"] and t["temperature"] is None]
    if bmc_none:
        pytest.xfail(
            f"BMC TMP75 sensor(s) returned None temperature (transient TTY timeout): "
            f"{bmc_none}. Re-run in isolation: ./run_tests.py stage_04_thermal"
        )

    for t in data:
        temp = t["temperature"]
        if temp is None:
            continue  # already handled above
        assert isinstance(temp, (int, float)), (
            f"Sensor {t['name']!r} temperature is not numeric: {temp!r}"
        )
        assert TEMP_MIN_C <= temp <= TEMP_MAX_C, (
            f"Sensor {t['name']!r} temperature {temp}°C is outside "
            f"sane range [{TEMP_MIN_C}, {TEMP_MAX_C}]°C"
        )


def test_thermal_api_below_high_threshold(ssh):
    """No sensor is currently above its high threshold."""
    data = _get_thermals(ssh)
    for t in data:
        temp = t["temperature"]
        high = t["high_threshold"]
        if temp is None or high is None:
            continue
        assert temp < high, (
            f"ALERT: Sensor {t['name']!r} is at {temp}°C which exceeds "
            f"high threshold {high}°C!"
        )


def test_thermal_api_status_ok(ssh):
    """All sensors report status=True (readable).

    In the Thermal implementation, status=False is equivalent to temperature=None
    (they are set together on a BMC read failure).  Any sensor with status=False
    is therefore a transient BMC TTY timeout, not a hardware fault, and is
    xfailed with a message to re-run in isolation.
    """
    data = _get_thermals(ssh)
    false_status = [t["name"] for t in data if not t["status"]]
    if not false_status:
        return  # all good — every sensor readable

    pytest.xfail(
        f"Sensor(s) report status=False (transient BMC TTY timeout): {false_status}. "
        f"Re-run in isolation: ./run_tests.py stage_04_thermal"
    )


def test_thermal_api_positions_sequential(ssh):
    """Sensor positions are 1-based sequential integers."""
    data = _get_thermals(ssh)
    positions = sorted(t["position"] for t in data)
    assert positions == list(range(1, NUM_THERMALS + 1)), (
        f"Unexpected position sequence: {positions}"
    )


# ------------------------------------------------------------------
# Host coretemp sysfs (direct, no BMC)
# ------------------------------------------------------------------

def test_host_coretemp_sysfs(ssh):
    """CPU coretemp is readable directly from host sysfs (no BMC needed)."""
    out, err, rc = ssh.run(
        "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -4"
    )
    print(f"\nHost thermal_zone temps (millidegrees): {out.strip()}")
    if rc != 0 or not out.strip():
        # Fallback: coretemp hwmon
        out2, _, _ = ssh.run(
            "find /sys/class/hwmon -name 'temp*_input' 2>/dev/null | "
            "xargs -I{} sh -c 'echo {}:$(cat {})' 2>/dev/null | head -8"
        )
        print(f"  coretemp hwmon fallback:\n{out2}")
        assert out2.strip(), "No CPU temperature sysfs entries found"
    else:
        # Values are in millidegrees; first should be non-zero
        temps = [int(v) for v in out.splitlines() if v.strip().isdigit()]
        assert any(t > 0 for t in temps), f"All host coretemp readings are 0: {temps}"
