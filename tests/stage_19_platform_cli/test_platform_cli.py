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


def test_base_mac_api(ssh):
    out, err, rc = ssh.run(
        'python3 -c "from sonic_platform.platform import Platform; '
        'ch = Platform().get_chassis(); print(ch.get_base_mac())"',
        timeout=30
    )
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


def test_port_cage_type_qsfp28(ssh):
    out, err, rc = ssh.run(
        'python3 -c "'
        'from sonic_platform.platform import Platform; '
        'from sonic_platform_base.sfp_base import SfpBase; '
        'ch = Platform().get_chassis(); '
        'print(ch.get_port_or_cage_type(1) == SfpBase.SFP_PORT_TYPE_BIT_QSFP28)"',
        timeout=30
    )
    assert rc == 0, f"get_port_or_cage_type raised: {err}"
    assert "True" in out, f"get_port_or_cage_type(1) did not return QSFP28 bitmask: {out}"


def test_watchdogutil_status(ssh):
    out, err, rc = ssh.run("watchdogutil status", timeout=15)
    assert rc == 0, f"watchdogutil status failed (rc={rc}): {err}"
    # Stub — output may indicate watchdog is not armed; that's acceptable
    print(f"\nwatchdogutil status output:\n{out}")
