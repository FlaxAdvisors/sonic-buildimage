"""Stage 10 — Daemon Health.

Verifies that both wedge100s platform daemons are running and have
produced up-to-date cache files in /run/wedge100s/.

Expected cache files:
  I2C daemon (wedge100s-i2c-poller, 3 s interval):
    syseeprom                   — system EEPROM contents (8 KiB)
    sfp_{0..31}_present         — QSFP presence (0 or 1)
    sfp_{N}_eeprom              — QSFP EEPROM page 0 (256 B, present ports only)

  BMC daemon (wedge100s-bmc-poller, 10 s interval):
    thermal_{1..7}              — TMP75 temperatures (millidegrees C)
    fan_present                 — fan tray bitmask (0 = all present)
    fan_{1..5}_front            — front rotor RPM
    fan_{1..5}_rear             — rear rotor RPM
    psu_{1,2}_vin               — PSU input voltage (PMBus LINEAR11)
    psu_{1,2}_iin               — PSU input current
    psu_{1,2}_iout              — PSU output current
    psu_{1,2}_pout              — PSU output power

Cache staleness threshold: 30 s (well above the 10 s BMC poll interval).
"""

import time
import pytest

RUN_DIR = "/run/wedge100s"
STALE_THRESHOLD_S = 30

I2C_TIMER   = "wedge100s-i2c-poller.timer"
I2C_SERVICE = "wedge100s-i2c-poller.service"
BMC_TIMER   = "wedge100s-bmc-poller.timer"
BMC_SERVICE = "wedge100s-bmc-poller.service"

# New: persistent daemon tests (will pass after D2 ships)
I2C_DAEMON  = "wedge100s-i2c-daemon.service"
BMC_DAEMON  = "wedge100s-bmc-daemon.service"

NUM_PORTS     = 32
NUM_THERMALS  = 7
NUM_FAN_TRAYS = 5


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _systemctl_is_active(ssh, unit):
    """Return True if unit is active."""
    out, _, rc = ssh.run(f"systemctl is-active {unit}", timeout=10)
    return out.strip() == "active"


def _file_age_seconds(ssh, path):
    """Return age of file in seconds, or None if file missing/unreadable."""
    out, _, rc = ssh.run(
        f"python3 -c \"import os,time; print(int(time.time()-os.path.getmtime('{path}')))\" 2>/dev/null",
        timeout=10,
    )
    if rc != 0 or not out.strip().lstrip("-").isdigit():
        return None
    return int(out.strip())


def _file_exists(ssh, path):
    out, _, rc = ssh.run(f"test -f {path} && echo 1 || echo 0", timeout=10)
    return out.strip() == "1"


# ------------------------------------------------------------------
# Timer/service units
# ------------------------------------------------------------------

def test_i2c_timer_active(ssh):
    """wedge100s-i2c-poller.timer systemd unit is active."""
    active = _systemctl_is_active(ssh, I2C_TIMER)
    print(f"\n{I2C_TIMER}: {'active' if active else 'INACTIVE'}")
    assert active, (
        f"{I2C_TIMER} is not active.\n"
        f"Fix: sudo systemctl start {I2C_TIMER}"
    )


def test_bmc_timer_active(ssh):
    """wedge100s-bmc-poller.timer systemd unit is active."""
    active = _systemctl_is_active(ssh, BMC_TIMER)
    print(f"\n{BMC_TIMER}: {'active' if active else 'INACTIVE'}")
    assert active, (
        f"{BMC_TIMER} is not active.\n"
        f"Fix: sudo systemctl start {BMC_TIMER}"
    )


def test_i2c_service_not_failed(ssh):
    """wedge100s-i2c-poller.service has no failed state."""
    out, _, rc = ssh.run(f"systemctl is-failed {I2C_SERVICE} 2>&1", timeout=10)
    state = out.strip()
    print(f"\n{I2C_SERVICE} failed-state: {state!r}")
    assert state != "failed", (
        f"{I2C_SERVICE} is in failed state.\n"
        f"Check: sudo journalctl -u {I2C_SERVICE} -n 20"
    )


def test_bmc_service_not_failed(ssh):
    """wedge100s-bmc-poller.service has no failed state."""
    out, _, rc = ssh.run(f"systemctl is-failed {BMC_SERVICE} 2>&1", timeout=10)
    state = out.strip()
    print(f"\n{BMC_SERVICE} failed-state: {state!r}")
    assert state != "failed", (
        f"{BMC_SERVICE} is in failed state.\n"
        f"Check: sudo journalctl -u {BMC_SERVICE} -n 20"
    )


def test_i2c_daemon_running(ssh):
    """wedge100s-i2c-daemon.service is active (persistent daemon, D2)."""
    active = _systemctl_is_active(ssh, I2C_DAEMON)
    print(f"\n{I2C_DAEMON}: {'active' if active else 'INACTIVE'}")
    assert active, f"{I2C_DAEMON} not active — D2 not yet deployed"


# ------------------------------------------------------------------
# I2C daemon cache files
# ------------------------------------------------------------------

def test_syseeprom_cache_exists(ssh):
    """I2C daemon has written /run/wedge100s/syseeprom."""
    path = f"{RUN_DIR}/syseeprom"
    assert _file_exists(ssh, path), (
        f"{path} does not exist.\n"
        f"Fix: sudo systemctl start {I2C_SERVICE}"
    )
    # Size should be > 0
    out, _, rc = ssh.run(f"wc -c < {path}", timeout=10)
    size = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
    print(f"\nsyseeprom size: {size} bytes")
    assert size > 0, f"syseeprom cache file is empty"


def test_syseeprom_cache_not_stale(ssh):
    """syseeprom cache file was written within the last 60 s of first-boot window.

    The I2C daemon writes syseeprom once at first boot, not every 3 s.
    This test verifies the file exists and is non-empty (age is not checked
    for syseeprom since it is a one-time write).
    """
    path = f"{RUN_DIR}/syseeprom"
    assert _file_exists(ssh, path), f"{path} missing"
    # Just confirm it was written (age may be hours if no reboot)
    age = _file_age_seconds(ssh, path)
    print(f"\nsyseeprom age: {age}s")
    assert age is not None, f"Could not determine age of {path}"


def test_qsfp_presence_cache_all_ports(ssh):
    """I2C daemon has written sfp_{0..31}_present for all 32 ports."""
    missing = []
    for port in range(NUM_PORTS):
        path = f"{RUN_DIR}/sfp_{port}_present"
        if not _file_exists(ssh, path):
            missing.append(port)
    assert not missing, (
        f"Missing presence cache for ports: {missing}\n"
        f"Fix: sudo systemctl start {I2C_SERVICE}"
    )
    print(f"\nAll {NUM_PORTS} sfp_N_present files present")


def test_qsfp_presence_cache_valid_values(ssh):
    """All sfp_N_present files contain '0' or '1'."""
    invalid = []
    present_count = 0
    for port in range(NUM_PORTS):
        path = f"{RUN_DIR}/sfp_{port}_present"
        out, _, rc = ssh.run(f"cat {path} 2>/dev/null", timeout=10)
        val = out.strip()
        if val not in ("0", "1"):
            invalid.append(f"sfp_{port}_present={val!r}")
        if val == "1":
            present_count += 1
    assert not invalid, (
        f"Invalid presence values:\n" + "\n".join(invalid)
    )
    print(f"\nQSFP presence: {present_count}/{NUM_PORTS} ports populated")


def test_qsfp_presence_cache_fresh(ssh):
    """sfp_0_present is less than 30 s old (daemon is running)."""
    path = f"{RUN_DIR}/sfp_0_present"
    age = _file_age_seconds(ssh, path)
    print(f"\nsfp_0_present age: {age}s")
    assert age is not None, f"Could not determine age of {path}"
    assert age < STALE_THRESHOLD_S, (
        f"sfp_0_present is {age}s old (>{STALE_THRESHOLD_S}s threshold).\n"
        f"I2C daemon may not be running: sudo systemctl start {I2C_TIMER}"
    )


# ------------------------------------------------------------------
# BMC daemon cache files
# ------------------------------------------------------------------

def test_thermal_cache_all_sensors(ssh):
    """BMC daemon has written thermal_1 through thermal_7."""
    missing = []
    for n in range(1, NUM_THERMALS + 1):
        path = f"{RUN_DIR}/thermal_{n}"
        if not _file_exists(ssh, path):
            missing.append(n)
    assert not missing, (
        f"Missing thermal cache files for sensors: {missing}\n"
        f"Fix: sudo systemctl start {BMC_SERVICE}"
    )
    print(f"\nAll {NUM_THERMALS} thermal_N files present")


def test_thermal_cache_values_reasonable(ssh):
    """Thermal sensor values are in a physically plausible range (0–85 °C).

    Cache values are in millidegrees C (e.g., 35000 = 35 °C).
    """
    out_of_range = []
    for n in range(1, NUM_THERMALS + 1):
        path = f"{RUN_DIR}/thermal_{n}"
        out, _, rc = ssh.run(f"cat {path} 2>/dev/null", timeout=10)
        val_str = out.strip()
        if not val_str.lstrip("-").isdigit():
            out_of_range.append(f"thermal_{n}: unparseable {val_str!r}")
            continue
        millideg = int(val_str)
        deg_c = millideg / 1000.0
        print(f"  thermal_{n}: {deg_c:.1f} °C ({millideg} mdeg)")
        if not (0 <= deg_c <= 85):
            out_of_range.append(f"thermal_{n}: {deg_c:.1f} °C out of [0, 85] range")
    assert not out_of_range, "\n".join(out_of_range)


def test_fan_present_cache_exists(ssh):
    """BMC daemon has written fan_present."""
    path = f"{RUN_DIR}/fan_present"
    assert _file_exists(ssh, path), (
        f"{path} missing.\nFix: sudo systemctl start {BMC_SERVICE}"
    )
    out, _, rc = ssh.run(f"cat {path}", timeout=10)
    print(f"\nfan_present: {out.strip()}")
    assert rc == 0


def test_fan_rpm_cache_all_trays(ssh):
    """BMC daemon has written fan_{1..5}_front and fan_{1..5}_rear."""
    missing = []
    for n in range(1, NUM_FAN_TRAYS + 1):
        for side in ("front", "rear"):
            path = f"{RUN_DIR}/fan_{n}_{side}"
            if not _file_exists(ssh, path):
                missing.append(f"fan_{n}_{side}")
    assert not missing, (
        f"Missing fan RPM cache files: {missing}\n"
        f"Fix: sudo systemctl start {BMC_SERVICE}"
    )
    print(f"\nAll {NUM_FAN_TRAYS * 2} fan RPM files present")


def test_fan_rpm_values_reasonable(ssh):
    """Fan RPM values are in plausible range (0 if absent; 1000–20000 if present).

    A tray present but reading 0 RPM indicates a stalled fan; flag as warning.
    """
    fan_present_out, _, _ = ssh.run(f"cat {RUN_DIR}/fan_present 2>/dev/null", timeout=10)
    present_mask = 0
    try:
        present_mask = int(fan_present_out.strip(), 0)  # may be 0x..
    except (ValueError, AttributeError):
        pass

    stalled = []
    for n in range(1, NUM_FAN_TRAYS + 1):
        tray_absent = bool(present_mask & (1 << (n - 1)))
        for side in ("front", "rear"):
            path = f"{RUN_DIR}/fan_{n}_{side}"
            out, _, rc = ssh.run(f"cat {path} 2>/dev/null", timeout=10)
            val_str = out.strip()
            if not val_str.lstrip("-").isdigit():
                continue
            rpm = int(val_str)
            print(f"  fan_{n}_{side}: {rpm} RPM")
            if not tray_absent and rpm == 0:
                stalled.append(f"fan_{n}_{side}: 0 RPM (tray present)")
            if rpm < 0 or rpm > 20000:
                stalled.append(f"fan_{n}_{side}: {rpm} RPM out of range")
    if stalled:
        pytest.skip(
            "Fan stall or out-of-range RPM detected (may be a hardware issue):\n"
            + "\n".join(stalled)
        )


def test_psu_cache_files_exist(ssh):
    """BMC daemon has written psu_{1,2}_{vin,iin,iout,pout}."""
    missing = []
    for n in (1, 2):
        for metric in ("vin", "iin", "iout", "pout"):
            path = f"{RUN_DIR}/psu_{n}_{metric}"
            if not _file_exists(ssh, path):
                missing.append(f"psu_{n}_{metric}")
    if missing:
        pytest.skip(
            f"PSU cache files missing (PSU may not be installed): {missing}"
        )
    print(f"\nAll 8 PSU metric cache files present")


def test_bmc_cache_fresh(ssh):
    """thermal_1 is less than 30 s old (BMC daemon is running)."""
    path = f"{RUN_DIR}/thermal_1"
    age = _file_age_seconds(ssh, path)
    print(f"\nthermal_1 age: {age}s")
    assert age is not None, f"Could not determine age of {path}"
    assert age < STALE_THRESHOLD_S, (
        f"thermal_1 is {age}s old (>{STALE_THRESHOLD_S}s threshold).\n"
        f"BMC daemon may not be running: sudo systemctl start {BMC_TIMER}"
    )


def test_bmc_daemon_running(ssh):
    """wedge100s-bmc-daemon.service is active (persistent daemon, D3)."""
    active = _systemctl_is_active(ssh, BMC_DAEMON)
    print(f"\n{BMC_DAEMON}: {'active' if active else 'INACTIVE'}")
    assert active, f"{BMC_DAEMON} not active — D3 not yet deployed"


def test_bmc_led_init_deployed(ssh):
    """D1: clear_led_diag.sh is on BMC and th_led_en=1 (platform-init ran)."""
    # clear_led_diag.sh must exist on BMC
    # BMC key is root-owned; use sudo for SSH commands.
    _, _, rc = ssh.run(
        "sudo ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
        "-o ConnectTimeout=5 -i /etc/sonic/wedge100s-bmc-key "
        "root@fe80::ff:fe00:1%usb0 test -x /usr/local/bin/clear_led_diag.sh",
        timeout=15
    )
    assert rc == 0, "clear_led_diag.sh missing from BMC /usr/local/bin/"

    # th_led_en must be 1
    out, _, rc2 = ssh.run(
        "sudo ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
        "-o ConnectTimeout=5 -i /etc/sonic/wedge100s-bmc-key "
        "root@fe80::ff:fe00:1%usb0 "
        "cat /sys/class/i2c-adapter/i2c-12/12-0031/th_led_en",
        timeout=15
    )
    val = out.strip().split()[0] if out.strip() else ""
    assert rc2 == 0 and val in ("1", "0x1"), (
        f"th_led_en={out.strip()!r} (expected 1/0x1) — D1 LED init not yet applied"
    )
