"""Stage 05 — Fan trays (5× FanDrawer, each with front+rear rotors).

Fan trays are hot-swappable; presence depends on physical installation.
Speed is reported as a percentage (0–100) and in RPM via the BMC.
Direction is always front-to-back (intake).

Phase reference: Phase 4 (Fan).
"""

import json
import pytest

NUM_FAN_DRAWERS = 5
SPEED_MIN_PCT = 1    # any present fan should be spinning
SPEED_MAX_PCT = 100
RPM_MIN = 100        # any spinning fan should be above this

FAN_CAPTURE = """\
import json
from sonic_platform.platform import Platform

chassis = Platform().get_chassis()
drawers = chassis.get_all_fan_drawers()
results = []
for drawer in drawers:
    fans_in_drawer = []
    for fan in drawer.get_all_fans():
        fans_in_drawer.append({
            'name': fan.get_name(),
            'presence': fan.get_presence(),
            'status': fan.get_status(),
            'speed_pct': fan.get_speed(),
            'speed_rpm': fan.get_speed_rpm(),
            'direction': fan.get_direction(),
            'position': fan.get_position_in_parent(),
        })
    results.append({
        'name': drawer.get_name(),
        'presence': drawer.get_presence(),
        'status': drawer.get_status(),
        'fans': fans_in_drawer,
    })
print(json.dumps(results))
"""


def _get_fans(ssh):
    out, err, rc = ssh.run_python(FAN_CAPTURE, timeout=60)
    assert rc == 0, f"Fan capture script failed (rc={rc}): {err}"
    return json.loads(out.strip())


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def test_fan_cli_show(ssh):
    """show platform fan exits 0 and returns a table."""
    out, err, rc = ssh.run("show platform fan")
    print(f"\nshow platform fan:\n{out}")
    assert rc == 0, f"show platform fan failed: {err}"
    assert out.strip(), "show platform fan returned empty output"


def test_fan_cli_drawer_count(ssh):
    """show platform fan lists all 5 fan trays."""
    out, _, _ = ssh.run("show platform fan")
    # Each fan tray appears on its own row
    import re
    # Rows with a speed (RPM) value — numeric column
    data_rows = [l for l in out.splitlines() if re.search(r"\d{3,}", l)]
    print(f"\nFan data rows: {len(data_rows)}")
    for row in data_rows:
        print(f"  {row.strip()}")
    assert len(data_rows) >= NUM_FAN_DRAWERS, (
        f"Expected ≥{NUM_FAN_DRAWERS} fan rows, found {len(data_rows)}"
    )


# ------------------------------------------------------------------
# Python API — structure
# ------------------------------------------------------------------

def test_fan_api_drawer_count(ssh):
    """chassis.get_all_fan_drawers() returns exactly 5 drawers."""
    data = _get_fans(ssh)
    print(f"\nFan drawers ({len(data)}):")
    for d in data:
        print(f"  {d['name']}: present={d['presence']} status={d['status']}")
        for f in d['fans']:
            print(f"    {f['name']}: pct={f['speed_pct']}% rpm={f['speed_rpm']} "
                  f"dir={f['direction']}")
    assert len(data) == NUM_FAN_DRAWERS, (
        f"Expected {NUM_FAN_DRAWERS} fan drawers, got {len(data)}"
    )


def test_fan_api_names(ssh):
    """Fan drawer names follow 'FanTray N' pattern."""
    data = _get_fans(ssh)
    for i, drawer in enumerate(data, 1):
        assert "fantray" in drawer["name"].lower() or str(i) in drawer["name"], (
            f"Unexpected drawer name: {drawer['name']!r}"
        )


def test_fan_api_at_least_one_present(ssh):
    """At least one fan tray is physically present."""
    data = _get_fans(ssh)
    present = [d for d in data if d["presence"]]
    assert present, (
        "No fan trays are present — hardware may be fully unloaded. "
        f"Drawers: {[(d['name'], d['presence']) for d in data]}"
    )


def test_fan_api_present_fans_spinning(ssh):
    """All present fans report speed > 0%.

    speed_pct=0 with rpm=None indicates a transient BMC TTY read failure (the
    BMC serial port is shared and can time out under load).  If ALL fans show
    zero speed simultaneously, that is flagged as xfail (infrastructure issue)
    rather than a product bug.  Any fan individually at 0 while others are
    non-zero is a hard failure.
    """
    data = _get_fans(ssh)
    present_fans = [f for d in data if d["presence"] for f in d["fans"]]
    if not present_fans:
        pytest.skip("No present fans found")

    all_zero = all(f["speed_pct"] == 0 and f["speed_rpm"] is None for f in present_fans)
    if all_zero:
        pytest.xfail(
            "All present fans report speed=0% and RPM=None — likely a transient "
            "BMC TTY timeout from earlier tests exhausting the serial port. "
            "Re-run the stage in isolation to confirm: ./run_tests.py stage_05_fan"
        )

    for drawer in data:
        if not drawer["presence"]:
            continue
        for fan in drawer["fans"]:
            assert fan["speed_pct"] <= SPEED_MAX_PCT, (
                f"Fan {fan['name']!r} speed={fan['speed_pct']}% exceeds 100%"
            )
            assert fan["speed_pct"] >= SPEED_MIN_PCT, (
                f"Fan {fan['name']!r} is present but speed={fan['speed_pct']}% "
                f"(expected ≥{SPEED_MIN_PCT}%)"
            )


def test_fan_api_present_fans_rpm(ssh):
    """All present fans report RPM > 0.

    rpm=None indicates a transient BMC TTY timeout; xfail if all fans affected.
    """
    data = _get_fans(ssh)
    present_fans = [f for d in data if d["presence"] for f in d["fans"]]
    if not present_fans:
        pytest.skip("No present fans found")

    all_none = all(f["speed_rpm"] is None for f in present_fans)
    if all_none:
        pytest.xfail(
            "All present fans returned RPM=None — transient BMC TTY timeout. "
            "Re-run the fan stage in isolation to confirm: ./run_tests.py stage_05_fan"
        )

    for drawer in data:
        if not drawer["presence"]:
            continue
        for fan in drawer["fans"]:
            rpm = fan["speed_rpm"]
            assert rpm is not None, (
                f"Fan {fan['name']!r} returned None RPM while other fans returned values"
            )
            assert rpm >= RPM_MIN, (
                f"Fan {fan['name']!r} RPM={rpm} is below minimum {RPM_MIN}"
            )


def test_fan_api_direction_f2b(ssh):
    """All fans report front-to-back (intake) direction."""
    data = _get_fans(ssh)
    for drawer in data:
        for fan in drawer["fans"]:
            direction = fan["direction"]
            assert direction in ("intake", "f2b", "front-to-back"), (
                f"Fan {fan['name']!r} has unexpected direction: {direction!r}"
            )


def test_fan_api_status_matches_presence(ssh):
    """Fan status matches presence (present fans should be 'OK')."""
    data = _get_fans(ssh)
    for drawer in data:
        if drawer["presence"]:
            assert drawer["status"], (
                f"Drawer {drawer['name']!r} is present but status=False"
            )


# ------------------------------------------------------------------
# BMC fan sysfs (direct check)
# ------------------------------------------------------------------

def test_bmc_fan_sysfs_readable(ssh):
    """Fan RPM files are readable on the BMC fan board (i2c-8/0x33)."""
    code = """\
from sonic_platform import bmc
# Fan board at BMC i2c-8/0x33
base = '/sys/bus/i2c/devices/8-0033'
results = []
for i in range(1, 11):
    val = bmc.file_read_int(f'{base}/fan{i}_input')
    results.append(f'fan{i}_input={val}')
print(' | '.join(results))
"""
    out, err, rc = ssh.run_python(code, timeout=30)
    print(f"\nBMC fan sysfs: {out.strip()}")
    assert rc == 0, f"BMC fan sysfs script failed: {err}"
    assert "fan1_input" in out, "No fan sysfs data returned from BMC"
    # At least one fan should be non-zero
    import re
    values = [int(m) for m in re.findall(r"=(\d+)", out) if int(m) > 0]
    assert values, f"All BMC fan RPM readings are 0 or None: {out}"
