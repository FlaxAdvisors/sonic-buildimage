"""Human-readable hardware state reporter for Wedge 100S-32X.

Each ``report_<stage>()`` function collects data from the live device via SSH
and prints formatted tables.  Called by ``run_tests.py --report``; the test
assertions in stage_XX/test_*.py are entirely separate.
"""

import json
import re
import sys

# ---------------------------------------------------------------------------
# ONIE TLV type codes
# ---------------------------------------------------------------------------

TLV_NAMES = {
    "0x21": "Product Name",
    "0x22": "Part Number",
    "0x23": "Serial Number",
    "0x24": "Base MAC Address",
    "0x25": "Manufacture Date",
    "0x26": "Device Version",
    "0x27": "Label Revision",
    "0x28": "Platform Name",
    "0x29": "ONIE Version",
    "0x2a": "MAC Addresses",
    "0x2b": "Manufacturer",
    "0x2c": "Country Code",
    "0x2d": "Vendor Name",
    "0x2e": "Diag Version",
    "0x2f": "Service Tag",
    "0xff": "CRC-32",
}

LED_NAMES = {0x00: "off", 0x01: "red", 0x02: "green", 0x04: "blue"}

# ---------------------------------------------------------------------------
# Table / output helpers
# ---------------------------------------------------------------------------

def _table(headers, rows, title=None, indent=2):
    """Print a fixed-width ASCII table."""
    pad = " " * indent
    if title:
        print(f"\n{pad}{title}:")
    if not rows:
        print(f"{pad}  (no data)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep    = "  ".join("-" * w for w in widths)
    print(f"{pad}{header}")
    print(f"{pad}{sep}")
    for row in rows:
        cells = list(row) + [""] * (len(headers) - len(row))
        line  = "  ".join(str(cells[i]).ljust(widths[i]) for i in range(len(headers)))
        print(f"{pad}{line}")


def _err(msg):
    print(f"  [!] {msg}", file=sys.stderr)


def _fmt(val, unit, decimals=1):
    """Format a numeric value + unit, or 'N/A'."""
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f} {unit}"


# ---------------------------------------------------------------------------
# Stage 01 — EEPROM
# ---------------------------------------------------------------------------

_EEPROM_SCRIPT = """\
import json
from sonic_platform.platform import Platform
print(json.dumps(Platform().get_chassis().get_system_eeprom_info()))
"""

def report_eeprom(ssh):
    """Stage 01 — System EEPROM TLV contents."""
    out, err, rc = ssh.run_python(_EEPROM_SCRIPT, timeout=30)
    if rc != 0 or not out.strip():
        _err(f"Python API failed ({err.strip()}) — falling back to decode-syseeprom")
        raw, _, _ = ssh.run("sudo decode-syseeprom")
        print(raw)
        return
    info = json.loads(out.strip())
    rows = []
    for code_hex, value in sorted(info.items(), key=lambda kv: kv[0].lower()):
        name = TLV_NAMES.get(code_hex.lower(), TLV_NAMES.get(code_hex, ""))
        rows.append((name or code_hex, code_hex, str(value)))
    _table(["TLV Name", "Code", "Value"], rows, title="System EEPROM")


# ---------------------------------------------------------------------------
# Stage 02 — System software
# ---------------------------------------------------------------------------

def report_system(ssh):
    """Stage 02 — Kernel, NOS version, platform identity, containers."""
    kernel, _, _  = ssh.run("uname -r")
    arch, _, _    = ssh.run("uname -m")
    ver_raw, _, _ = ssh.run("cat /etc/sonic/sonic_version.yml 2>/dev/null")

    build_ver = "unknown"
    for line in ver_raw.splitlines():
        if "build_version" in line:
            build_ver = line.split(":", 1)[-1].strip().strip("'\"")
            break

    plat_out, _, _ = ssh.run("show platform summary 2>/dev/null")
    hw_sku   = "unknown"
    platform = "unknown"
    for line in plat_out.splitlines():
        ll = line.lower()
        if "hwsku" in ll or "hw sku" in ll:
            hw_sku = line.split(":", 1)[-1].strip()
        if ll.startswith("platform"):
            platform = line.split(":", 1)[-1].strip()

    _table(
        ["Property", "Value"],
        [
            ("Kernel",    kernel.strip()),
            ("Arch",      arch.strip()),
            ("NOS Build", build_ver),
            ("Platform",  platform),
            ("HW SKU",    hw_sku),
        ],
        title="Software / Hardware Identity",
    )

    cont_out, _, _ = ssh.run(
        "docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null"
    )
    rows = []
    for line in cont_out.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append(tuple(parts))
    _table(["Container", "Status", "Image"], rows, title="Running Containers")


# ---------------------------------------------------------------------------
# Stage 03 — I2C topology + BMC
# ---------------------------------------------------------------------------

def report_i2c_bmc(ssh):
    """Stage 03 — I2C bus topology and BMC status."""
    buses_out, _, _ = ssh.run("ls /dev/i2c-* 2>/dev/null")
    buses = [b.strip() for b in buses_out.splitlines() if b.strip()]
    first, last = (buses[0], buses[-1]) if buses else ("?", "?")
    print(f"\n  I2C buses: {len(buses)} devices  ({first} … {last})")

    adapt_out, _, _ = ssh.run("sudo i2cdetect -l 2>/dev/null")
    rows = []
    for line in adapt_out.strip().splitlines():
        m = re.match(r"(i2c-\d+)\s+\S+\s+(.+?)\s{2,}", line)
        if m:
            rows.append((m.group(1), m.group(2).strip()))
    if rows:
        shown = rows[:16]
        _table(["Bus", "Adapter Description"], shown, title="I2C Adapters")
        if len(rows) > 16:
            print(f"      … and {len(rows) - 16} more")

    # CPLD registers
    regs = [
        ("PSU Status",  "0x10"),
        ("SYS1 LED",    "0x3e"),
        ("SYS2 LED",    "0x3f"),
    ]
    cpld_rows = []
    for label, reg in regs:
        val, _, rc = ssh.run(f"sudo i2cget -y 1 0x32 {reg} 2>/dev/null")
        cpld_rows.append((label, reg, val.strip() if rc == 0 else "read error"))
    _table(["Register", "Offset", "Raw Value"], cpld_rows, title="CPLD (i2c-1/0x32)")

    # BMC
    bmc_code = """\
from sonic_platform import bmc
result = bmc.send_command('uptime') or 'NO RESPONSE'
print(result.strip()[:120])
"""
    bmc_out, _, _ = ssh.run_python(bmc_code, timeout=25)
    print(f"\n  BMC uptime : {bmc_out.strip() or 'no response'}")

    tty, _, _ = ssh.run("ls /dev/ttyACM* 2>/dev/null || echo MISSING")
    present = "present" if "ttyACM" in tty else "MISSING"
    print(f"  BMC TTY    : {present}  ({tty.strip()})")

    svc, _, _ = ssh.run(
        "systemctl show wedge100s-platform-init.service "
        "--property=ActiveState,Result --value 2>/dev/null | paste - -"
    )
    print(f"  platform-init svc: {svc.strip()}")


# ---------------------------------------------------------------------------
# Stage 04 — Thermal
# ---------------------------------------------------------------------------

_THERMAL_SCRIPT = """\
import json
from sonic_platform.platform import Platform
out = []
for t in Platform().get_chassis().get_all_thermals():
    out.append({
        'name': t.get_name(),
        'temp': t.get_temperature(),
        'high': t.get_high_threshold(),
        'crit': t.get_high_critical_threshold(),
        'ok':   t.get_status(),
    })
print(json.dumps(out))
"""

def report_thermal(ssh):
    """Stage 04 — Thermal sensors."""
    out, err, rc = ssh.run_python(_THERMAL_SCRIPT, timeout=60)
    if rc != 0 or not out.strip():
        _err(f"Thermal API failed: {err.strip()}")
        raw, _, _ = ssh.run("show platform temperature 2>/dev/null")
        print(raw)
        return

    data = json.loads(out.strip())
    rows = []
    for t in data:
        temp = t["temp"]
        high = t["high"]
        note = ""
        if temp is not None and high is not None and temp >= high * 0.90:
            note = "NEAR LIMIT"
        rows.append((
            t["name"],
            f"{temp:.1f} °C"  if temp is not None else "N/A",
            f"{high:.1f} °C"  if high is not None else "N/A",
            f"{t['crit']:.1f} °C" if t["crit"] is not None else "N/A",
            "OK" if t["ok"] else "FAIL",
            note,
        ))
    _table(
        ["Sensor", "Temp", "High Thresh", "Crit Thresh", "Status", "Note"],
        rows,
        title="Thermal Sensors",
    )


# ---------------------------------------------------------------------------
# Stage 05 — Fan
# ---------------------------------------------------------------------------

_FAN_SCRIPT = """\
import json
from sonic_platform.platform import Platform
chassis = Platform().get_chassis()
rows = []
for drawer in chassis.get_all_fan_drawers():
    for fan in drawer.get_all_fans():
        rows.append({
            'tray':      drawer.get_name(),
            'present':   fan.get_presence(),
            'status':    fan.get_status(),
            'speed_pct': fan.get_speed(),
            'speed_rpm': fan.get_speed_rpm(),
            'direction': fan.get_direction(),
        })
print(json.dumps(rows))
"""

def report_fan(ssh):
    """Stage 05 — Fan trays."""
    out, err, rc = ssh.run_python(_FAN_SCRIPT, timeout=60)
    if rc != 0 or not out.strip():
        _err(f"Fan API failed: {err.strip()}")
        raw, _, _ = ssh.run("show platform fan 2>/dev/null")
        print(raw)
        return

    data = json.loads(out.strip())
    rows = []
    for f in data:
        present = f["present"]
        rows.append((
            f["tray"],
            "Yes" if present else "No",
            "OK"  if f["status"] else "FAIL",
            f'{f["speed_pct"]} %'   if present else "--",
            f'{f["speed_rpm"]} RPM' if f["speed_rpm"] is not None else "--",
            f["direction"]          if present else "--",
        ))
    _table(
        ["Fan Tray", "Present", "Status", "Speed %", "Speed RPM", "Direction"],
        rows,
        title="Fan Trays",
    )


# ---------------------------------------------------------------------------
# Stage 06 — PSU
# ---------------------------------------------------------------------------

_PSU_SCRIPT = """\
import json
from sonic_platform.platform import Platform
chassis = Platform().get_chassis()
rows = []
for psu in chassis.get_all_psus():
    rows.append({
        'name':    psu.get_name(),
        'present': psu.get_presence(),
        'status':  psu.get_status(),
        'cap_w':   psu.get_capacity(),
        'v_out':   psu.get_voltage(),
        'a_out':   psu.get_current(),
        'w_out':   psu.get_power(),
        'v_in':    psu.get_input_voltage(),
        'a_in':    psu.get_input_current(),
    })
print(json.dumps(rows))
"""

def report_psu(ssh):
    """Stage 06 — Power supplies."""
    out, err, rc = ssh.run_python(_PSU_SCRIPT, timeout=60)
    if rc != 0 or not out.strip():
        _err(f"PSU API failed: {err.strip()}")
        raw, _, _ = ssh.run("show platform psustatus 2>/dev/null")
        print(raw)
        return

    data = json.loads(out.strip())
    rows = []
    for p in data:
        rows.append((
            p["name"],
            "Yes" if p["present"] else "No",
            "OK"  if p["status"]  else "FAIL",
            _fmt(p["v_in"],  "V AC"),
            _fmt(p["a_in"],  "A"),
            _fmt(p["v_out"], "V DC"),
            _fmt(p["a_out"], "A"),
            _fmt(p["w_out"], "W"),
            _fmt(p["cap_w"], "W", 0),
        ))
    _table(
        ["PSU", "Present", "Status",
         "AC Vin", "AC Iin", "DC Vout", "DC Iout", "DC Pout", "Capacity"],
        rows,
        title="Power Supplies",
    )


# ---------------------------------------------------------------------------
# Stage 07 — QSFP
# ---------------------------------------------------------------------------

_QSFP_SCRIPT = """\
import json
from sonic_platform.platform import Platform
chassis = Platform().get_chassis()
rows = []
for idx in range(1, 33):
    sfp = chassis.get_sfp(idx)
    present = sfp.get_presence()
    rows.append({
        'index':   idx,
        'name':    sfp.get_name(),
        'present': present,
        'error':   sfp.get_error_description(),
    })
print(json.dumps(rows))
"""

def report_qsfp(ssh):
    """Stage 07 — QSFP28 presence grid and present-port details."""
    out, err, rc = ssh.run_python(_QSFP_SCRIPT, timeout=90)
    if rc != 0 or not out.strip():
        _err(f"QSFP API failed: {err.strip()}")
        raw, _, _ = ssh.run("show interfaces transceiver presence 2>/dev/null")
        print(raw)
        return

    data    = json.loads(out.strip())
    present = [p for p in data if p["present"]]
    absent  = [p for p in data if not p["present"]]

    print(f"\n  QSFP Presence: {len(present)} present, {len(absent)} absent  (of 32 ports)")

    # Compact 8-wide grid
    print("\n  Port grid (ports 1–32)   P = present   . = absent")
    for i, p in enumerate(data, 1):
        if (i - 1) % 8 == 0:
            print(f"\n    {i:2d}–{min(i + 7, 32):2d}:  ", end="")
        print("P " if p["present"] else ". ", end="")
    print()

    if present:
        _table(
            ["Index", "Name", "Error Description"],
            [(p["index"], p["name"], p["error"]) for p in present],
            title="Present Modules",
        )


# ---------------------------------------------------------------------------
# Stage 08 — LED
# ---------------------------------------------------------------------------

def report_led(ssh):
    """Stage 08 — System LED states (CPLD i2c-1/0x32) and ledd daemon."""
    regs = [
        ("SYS1 (system-status)", 0x3E),
        ("SYS2 (port-activity)",  0x3F),
    ]
    rows = []
    for label, reg in regs:
        raw, _, rc = ssh.run(f"sudo i2cget -y 1 0x32 0x{reg:02x} 2>/dev/null")
        if rc != 0:
            rows.append((label, f"0x{reg:02x}", "--", "read error"))
            continue
        m = re.match(r"0x([0-9a-fA-F]{2})", raw.strip())
        if m:
            val   = int(m.group(1), 16)
            state = LED_NAMES.get(val, f"unknown (0x{val:02x})")
            rows.append((label, f"0x{reg:02x}", f"0x{val:02x}", state))
        else:
            rows.append((label, f"0x{reg:02x}", raw.strip(), "parse error"))
    _table(["LED", "Reg", "Raw", "State"], rows, title="System LEDs (CPLD i2c-1/0x32)")

    link_out, _, _ = ssh.run(
        "show interfaces status 2>/dev/null | grep -c ' up ' || echo 0"
    )
    print(f"\n  Ports with link-up : {link_out.strip()}")

    ledd_out, _, _ = ssh.run(
        "docker exec pmon supervisorctl status ledd 2>/dev/null || echo N/A"
    )
    print(f"  ledd daemon        : {ledd_out.strip()}")

    ctrl_file = (
        "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
        "/pmon_daemon_control.json"
    )
    ctrl, _, rc = ssh.run(f"cat {ctrl_file} 2>/dev/null || echo MISSING")
    print(f"  pmon_daemon_control: {'present' if rc == 0 else 'MISSING'}")
    if rc == 0:
        print(f"    {ctrl.strip()}")


# ---------------------------------------------------------------------------
# Registry: stage name → reporter function
# ---------------------------------------------------------------------------

REPORTERS = {
    "stage_01_eeprom":   report_eeprom,
    "stage_02_system":   report_system,
    "stage_03_i2c_bmc":  report_i2c_bmc,
    "stage_04_thermal":  report_thermal,
    "stage_05_fan":      report_fan,
    "stage_06_psu":      report_psu,
    "stage_07_qsfp":     report_qsfp,
    "stage_08_led":      report_led,
}
