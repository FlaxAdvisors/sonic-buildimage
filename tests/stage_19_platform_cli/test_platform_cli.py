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
    # 'show platform reboot-cause' does not exist on this SONiC version;
    # the correct command is 'show reboot-cause'.
    out, err, rc = ssh.run("show reboot-cause", timeout=30)
    assert rc == 0, f"show reboot-cause failed: {err}"
    assert out.strip(), "show reboot-cause returned empty output"


def test_firmware_cpld(ssh):
    out, err, rc = ssh.run(
        'python3 -c "from sonic_platform.platform import Platform; '
        'ch = Platform().get_chassis(); '
        'components = ch.get_all_components(); '
        'cpld = next((c for c in components if c.get_name() == \\"CPLD\\"), None); '
        'print(cpld.get_firmware_version() if cpld else \\"ABSENT\\")"',
        timeout=30
    )
    assert rc == 0, f"get_firmware_version(CPLD) raised: {err}"
    version = out.strip()
    assert version and version != "ABSENT" and not version.startswith("N/A ("), (
        f"CPLD version invalid: {version!r}"
    )


def test_firmware_bios(ssh):
    out, err, rc = ssh.run(
        'python3 -c "from sonic_platform.platform import Platform; '
        'ch = Platform().get_chassis(); '
        'components = ch.get_all_components(); '
        'bios = next((c for c in components if c.get_name() == \\"BIOS\\"), None); '
        'print(bios.get_firmware_version() if bios else \\"ABSENT\\")"',
        timeout=30
    )
    assert rc == 0, f"get_firmware_version(BIOS) raised: {err}"
    version = out.strip()
    assert version and version != "ABSENT" and not version.startswith("N/A ("), (
        f"BIOS version invalid: {version!r}"
    )


def test_psu_model_not_na(ssh):
    out, err, rc = ssh.run(
        'python3 -c "from sonic_platform.platform import Platform; '
        'ch = Platform().get_chassis(); '
        '[print(p.get_model()) for p in ch.get_all_psus()]"',
        timeout=30
    )
    assert rc == 0, f"get_model() raised: {err}"
    models = [l.strip() for l in out.strip().splitlines() if l.strip()]
    assert models, "No PSU models returned"
    for m in models:
        assert m not in ("N/A", "NA", ""), f"PSU model is N/A: {m!r}"


def test_environment_thermals(ssh):
    # 'show environment' on this platform only reports coretemp (CPU package/cores).
    # BMC sensors are not exported via lm-sensors. Expect at least 3 thermal lines.
    out, err, rc = ssh.run("show environment", timeout=30)
    assert rc == 0, f"show environment failed: {err}"
    temp_lines = [l for l in out.splitlines() if "°C" in l or "Degrees" in l or "TMP" in l]
    assert len(temp_lines) >= 3, (
        f"Expected >= 3 thermal sensor lines (coretemp), found {len(temp_lines)}:\n{out}"
    )


def test_environment_fans(ssh):
    # 'show environment' does not report fan data on this platform.
    # Fan data is exposed via 'show platform fan'; verify that instead.
    out, err, rc = ssh.run("show platform fan", timeout=30)
    assert rc == 0, f"show platform fan failed: {err}"
    fan_lines = [l for l in out.splitlines() if "FanTray" in l or ("Fan" in l and "%" in l)]
    assert len(fan_lines) >= 5, (
        f"Expected >= 5 fan lines from 'show platform fan', found {len(fan_lines)}:\n{out}"
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
    # watchdogutil requires root on this SONiC version.
    out, err, rc = ssh.run("sudo watchdogutil status", timeout=15)
    assert rc == 0, f"sudo watchdogutil status failed (rc={rc}): {err}"
    # Output may indicate watchdog is not armed; that's acceptable
    print(f"\nwatchdogutil status output:\n{out}")
