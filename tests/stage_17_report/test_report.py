"""Stage 18 — Platform Status Report.

Generates tests/reports/PLATFORM_STATUS_<date>.md with a complete
snapshot of the platform state.  This stage always passes — it is
a data-collection step, not an assertion stage.

Output sections:
  1. Hardware inventory (ports, PSUs, fan trays)
  2. Subsystem health (daemon status, cache file ages, sensor values)
  3. All-32-port link table (admin / oper / speed / transceiver type)
  4. EEPROM validity (magic bytes + TLV check)
  5. Per-stage pass/fail summary (from pytest last-run if available)

Output file: tests/reports/PLATFORM_STATUS_<YYYY-MM-DD>.md
             (relative to this file's parent directory)
"""

import os
import re
import datetime
import pytest

NUM_PORTS = 32
RUN_DIR   = "/run/wedge100s"

REPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


# ------------------------------------------------------------------
# Data collection helpers
# ------------------------------------------------------------------

def _run(ssh, cmd, timeout=30):
    out, _, _ = ssh.run(cmd, timeout=timeout)
    return out.strip()


def _collect_inventory(ssh):
    """Return dict with hardware inventory."""
    inv = {}

    # Port presence from daemon cache
    present = []
    for port in range(NUM_PORTS):
        val = _run(ssh, f"cat {RUN_DIR}/sfp_{port}_present 2>/dev/null", timeout=5)
        if val == "1":
            present.append(port)
    inv["qsfp_present_ports"] = present
    inv["qsfp_present_count"] = len(present)

    # PSU presence from CPLD sysfs
    for n in (1, 2):
        val = _run(ssh, f"cat /sys/bus/i2c/devices/1-0032/psu{n}_present 2>/dev/null")
        inv[f"psu{n}_present"] = val == "1"
        val = _run(ssh, f"cat /sys/bus/i2c/devices/1-0032/psu{n}_pgood 2>/dev/null")
        inv[f"psu{n}_pgood"] = val == "1"

    # Fan trays
    fan_mask_str = _run(ssh, f"cat {RUN_DIR}/fan_present 2>/dev/null")
    try:
        mask = int(fan_mask_str, 0)
    except (ValueError, TypeError):
        mask = 0xFF
    inv["fan_trays_present"] = sum(1 for i in range(5) if not (mask & (1 << i)))
    inv["fan_tray_mask_raw"] = fan_mask_str

    return inv


def _collect_daemon_health(ssh):
    """Return dict with daemon/timer status and cache file ages."""
    health = {}
    for unit in (
        "wedge100s-i2c-daemon.service",
        "wedge100s-bmc-daemon.service",
        "wedge100s-flex-counter-daemon.service",
        "wedge100s-platform-init.service",
        "wedge100s-pre-shutdown.service",
    ):
        out = _run(ssh, f"systemctl is-active {unit} 2>/dev/null", timeout=10)
        health[unit] = out

    # Cache file ages
    age_script = (
        "import os, time; "
        "files = ['syseeprom', 'sfp_0_present', 'thermal_1', 'fan_1_front']; "
        "[print(f, int(time.time() - os.path.getmtime(f'/run/wedge100s/{f}'))) "
        " if os.path.exists(f'/run/wedge100s/{f}') else print(f, 'MISSING') "
        " for f in files]"
    )
    out, _, rc = ssh.run_python(age_script, timeout=15)
    health["cache_ages"] = out.strip() if rc == 0 else "unavailable"
    return health


def _collect_sensor_values(ssh):
    """Return dict with thermal and fan sensor values."""
    sensors = {}
    for n in range(1, 8):
        val = _run(ssh, f"cat {RUN_DIR}/thermal_{n} 2>/dev/null", timeout=5)
        try:
            sensors[f"thermal_{n}_c"] = f"{int(val) / 1000.0:.1f}"
        except (ValueError, TypeError):
            sensors[f"thermal_{n}_c"] = "N/A"

    for n in range(1, 6):
        for side in ("front", "rear"):
            val = _run(ssh, f"cat {RUN_DIR}/fan_{n}_{side} 2>/dev/null", timeout=5)
            sensors[f"fan_{n}_{side}_rpm"] = val or "N/A"

    return sensors


def _collect_port_table(ssh):
    """Return list of dicts with port status from show interfaces status."""
    out = _run(ssh, "show interfaces status 2>/dev/null", timeout=30)
    ports = []
    for line in out.splitlines():
        m = re.match(
            r"\s*(Ethernet\d+)\s+"
            r"[\d,]+\s+"
            r"(\S+)\s+"   # speed
            r"\d+\s+"
            r"(\S+)\s+"   # fec
            r"\S+\s+"
            r"(\S+)\s+"   # type
            r"(\S+)\s+"   # oper
            r"(\S+)",     # admin
            line,
        )
        if m:
            ports.append({
                "name":  m.group(1),
                "speed": m.group(2),
                "fec":   m.group(3),
                "type":  m.group(4),
                "oper":  m.group(5),
                "admin": m.group(6),
            })
    return ports


def _collect_eeprom_validity(ssh):
    """Return eeprom status dict."""
    result = {}
    # Check syseeprom cache exists and has ONIE TLV magic
    out, _, rc = ssh.run(
        f"python3 -c \""
        "import struct; "
        "d = open('/run/wedge100s/syseeprom','rb').read(11); "
        "magic = d[0:8]; crc_hdr = d[8]; "
        "print('magic=' + magic.hex() + ' type=' + hex(crc_hdr))"
        "\" 2>&1",
        timeout=15,
    )
    result["syseeprom_cache"] = out.strip() if rc == 0 else "unavailable"

    # onie-syseeprom CLI check
    cli_out, _, rc = ssh.run("sudo onie-syseeprom 2>&1", timeout=15)
    result["onie_syseeprom_rc"] = rc
    result["onie_syseeprom_lines"] = len(cli_out.splitlines())
    result["onie_syseeprom_snippet"] = "\n".join(cli_out.splitlines()[:5])
    return result


def _collect_pmon_status(ssh):
    """Return pmon container and daemon status."""
    pmon = {}
    out = _run(ssh, "docker ps --format '{{.Names}}' --filter name=pmon", timeout=10)
    pmon["running"] = "pmon" in out
    if pmon["running"]:
        svctl = _run(ssh, "docker exec pmon supervisorctl status 2>/dev/null", timeout=15)
        pmon["daemons"] = svctl
    return pmon


# ------------------------------------------------------------------
# Report generation
# ------------------------------------------------------------------

def test_generate_platform_status_report(ssh):
    """Generate PLATFORM_STATUS_<date>.md in tests/reports/.

    This test always passes — it collects data and writes the report file.
    """
    date_str = datetime.date.today().isoformat()
    report_path = os.path.join(REPORT_DIR, f"PLATFORM_STATUS_{date_str}.md")
    os.makedirs(REPORT_DIR, exist_ok=True)

    inv     = _collect_inventory(ssh)
    health  = _collect_daemon_health(ssh)
    sensors = _collect_sensor_values(ssh)
    ports   = _collect_port_table(ssh)
    eeprom  = _collect_eeprom_validity(ssh)
    pmon    = _collect_pmon_status(ssh)

    lines = []
    def h(text, level=1):
        lines.append("#" * level + " " + text)
        lines.append("")

    def row(*cols):
        lines.append("| " + " | ".join(str(c) for c in cols) + " |")

    def sep(*widths):
        lines.append("| " + " | ".join("-" * w for w in widths) + " |")

    # --- Header ---
    h(f"Platform Status Report — Wedge 100S-32X")
    lines.append(f"**Date:** {date_str}  ")
    lines.append(f"**Generated by:** stage_17_report/test_report.py")
    lines.append("")

    # --- Hardware Inventory ---
    h("Hardware Inventory", 2)
    row("Item", "Value")
    sep(30, 20)
    row("QSFP ports populated", f"{inv['qsfp_present_count']} / {NUM_PORTS}")
    row("PSU 1 present",        "Yes" if inv["psu1_present"] else "No")
    row("PSU 1 power good",     "Yes" if inv["psu1_pgood"] else "No")
    row("PSU 2 present",        "Yes" if inv["psu2_present"] else "No")
    row("PSU 2 power good",     "Yes" if inv["psu2_pgood"] else "No")
    row("Fan trays present",    f"{inv['fan_trays_present']} / 5 (mask={inv['fan_tray_mask_raw']})")
    lines.append("")

    if inv["qsfp_present_ports"]:
        lines.append(f"**Populated QSFP ports (0-indexed):** {inv['qsfp_present_ports']}")
        lines.append("")

    # --- Daemon Health ---
    h("Daemon Health", 2)
    row("Unit", "State")
    sep(45, 10)
    for unit, state in health.items():
        if unit != "cache_ages":
            row(unit, state)
    lines.append("")
    lines.append("**Cache file ages (seconds):**")
    lines.append("```")
    lines.append(health.get("cache_ages", "unavailable"))
    lines.append("```")
    lines.append("")

    # --- Sensor Values ---
    h("Sensor Values", 2)
    row("Sensor", "Value")
    sep(25, 15)
    for n in range(1, 8):
        row(f"thermal_{n}", f"{sensors.get(f'thermal_{n}_c', 'N/A')} °C")
    lines.append("")
    row("Fan", "Front RPM", "Rear RPM")
    sep(8, 12, 12)
    for n in range(1, 6):
        row(f"fan_{n}",
            sensors.get(f"fan_{n}_front_rpm", "N/A"),
            sensors.get(f"fan_{n}_rear_rpm", "N/A"))
    lines.append("")

    # --- Port Link Table ---
    h("Port Link Table (all 32 ports)", 2)
    row("Port", "Admin", "Oper", "Speed", "FEC", "Type")
    sep(12, 7, 6, 8, 6, 12)
    for p in sorted(ports, key=lambda x: int(re.search(r"\d+", x["name"]).group())):
        row(p["name"], p["admin"], p["oper"], p["speed"], p["fec"], p["type"])
    up_count = sum(1 for p in ports if p["oper"] == "up")
    lines.append("")
    lines.append(f"**Links up: {up_count} / {len(ports)}**")
    lines.append("")

    # --- EEPROM ---
    h("System EEPROM", 2)
    row("Check", "Result")
    sep(30, 40)
    row("syseeprom cache", eeprom["syseeprom_cache"])
    row("onie-syseeprom rc", eeprom["onie_syseeprom_rc"])
    row("onie-syseeprom lines", eeprom["onie_syseeprom_lines"])
    lines.append("")
    lines.append("**onie-syseeprom (first 5 lines):**")
    lines.append("```")
    lines.append(eeprom["onie_syseeprom_snippet"])
    lines.append("```")
    lines.append("")

    # --- pmon ---
    h("pmon Container", 2)
    lines.append(f"**pmon running:** {'Yes' if pmon['running'] else 'NO'}")
    lines.append("")
    if pmon.get("daemons"):
        lines.append("**Daemon status:**")
        lines.append("```")
        lines.append(pmon["daemons"])
        lines.append("```")
    lines.append("")

    # Write file
    content = "\n".join(lines) + "\n"
    with open(report_path, "w") as f:
        f.write(content)

    print(f"\nReport written to: {report_path}")
    print(f"  Ports up: {up_count}/{len(ports)}")
    print(f"  QSFP populated: {inv['qsfp_present_count']}/{NUM_PORTS}")
    print(f"  PSU1: present={inv['psu1_present']} pgood={inv['psu1_pgood']}")
    print(f"  PSU2: present={inv['psu2_present']} pgood={inv['psu2_pgood']}")

    assert os.path.exists(report_path), "Report file was not created"
