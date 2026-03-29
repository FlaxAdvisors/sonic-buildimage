"""Human-readable hardware state reporter for Wedge 100S-32X.

Each ``report_<stage>()`` function collects data from the live device via SSH
and prints formatted tables.  Called by ``run_tests.py --report``; the test
assertions in stage_XX/test_*.py are entirely separate.
"""

import json
import os
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

def report_platform(ssh):
    """Stage 03 — Platform infrastructure: I2C topology, BMC TTY, and daemon health."""
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

    # CPLD LED registers via sysfs (wedge100s_cpld driver holds the device)
    sysfs = '/sys/bus/i2c/devices/1-0032'
    cpld_rows = []
    for label, attr, reg in [("SYS1 LED", 'led_sys1', "0x3e"), ("SYS2 LED", 'led_sys2', "0x3f")]:
        val, _, rc = ssh.run(f"cat {sysfs}/{attr} 2>/dev/null")
        if rc == 0:
            try:
                iv = int(val.strip(), 0)
                cpld_rows.append((label, reg, f"0x{iv:02x}"))
            except ValueError:
                cpld_rows.append((label, reg, val.strip()))
        else:
            cpld_rows.append((label, reg, "read error"))
    _table(["Register", "Offset", "Raw Value"], cpld_rows, title="CPLD LEDs (sysfs)")

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

    # wedge100s-bmc-poller and /run/wedge100s health
    timer_state, _, _ = ssh.run(
        "systemctl is-active wedge100s-bmc-poller.timer 2>/dev/null || echo unknown"
    )
    print(f"  bmc-poller timer : {timer_state.strip()}")

    files_out, _, rc = ssh.run("ls /run/wedge100s/ 2>/dev/null | wc -l")
    file_count = files_out.strip() if rc == 0 else "?"
    print(f"  /run/wedge100s/  : {file_count} file(s)")


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
    """Stage 08 — System LED states (wedge100s_cpld sysfs) and ledd daemon."""
    sysfs = '/sys/bus/i2c/devices/1-0032'
    leds = [
        ("SYS1 (system-status)", 'led_sys1', 0x3E),
        ("SYS2 (port-activity)",  'led_sys2', 0x3F),
    ]
    rows = []
    for label, attr, reg in leds:
        raw, _, rc = ssh.run(f"cat {sysfs}/{attr} 2>/dev/null")
        if rc != 0:
            rows.append((label, f"0x{reg:02x}", "--", "read error"))
            continue
        try:
            val   = int(raw.strip(), 0)
            state = LED_NAMES.get(val, f"unknown (0x{val:02x})")
            rows.append((label, f"0x{reg:02x}", f"0x{val:02x}", state))
        except ValueError:
            rows.append((label, f"0x{reg:02x}", raw.strip(), "parse error"))
    _table(["LED", "Reg", "Raw", "State"], rows, title="System LEDs (CPLD sysfs)")

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
# Stage 13 — Link status
# ---------------------------------------------------------------------------

_CONNECTED_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]

def report_link(ssh):
    """Stage 13 — Link status and port state pipeline for connected ports."""
    status_out, _, _ = ssh.run("show interfaces status 2>/dev/null", timeout=30)
    rows = []
    for line in status_out.splitlines():
        m = re.match(
            r"\s*(Ethernet\d+)\s+[\d,]+\s+(\S+)\s+\d+\s+(\S+)\s+\S+\s+(\S+)\s+(\S+)\s+(\S+)",
            line,
        )
        if m and m.group(1) in _CONNECTED_PORTS:
            rows.append((m.group(1), m.group(2), m.group(3), m.group(5), m.group(6)))
    _table(
        ["Port", "Speed", "FEC", "Oper", "Admin"],
        rows,
        title="Connected Ports (100G DAC to rabbit-lorax)",
    )

    # SYS2 LED via sysfs
    raw, _, rc = ssh.run("cat /sys/bus/i2c/devices/1-0032/led_sys2 2>/dev/null")
    if rc == 0:
        try:
            val   = int(raw.strip(), 0)
            state = LED_NAMES.get(val, f"unknown (0x{val:02x})")
            print(f"\n  SYS2 LED (port-activity): 0x{val:02x} = {state}")
        except ValueError:
            print(f"\n  SYS2 LED: {raw.strip()}")
    else:
        print("\n  SYS2 LED: read error")

    lldp_out, _, _ = ssh.run("show lldp neighbors 2>/dev/null", timeout=15)
    neighbors = [l for l in lldp_out.strip().splitlines() if l.strip()]
    print(f"\n  LLDP neighbors: {len(neighbors)} line(s)")
    for line in neighbors[:12]:
        print(f"    {line}")
    if len(neighbors) > 12:
        print(f"    … and {len(neighbors) - 12} more")


# ---------------------------------------------------------------------------
# Stage 09 — CPLD
# ---------------------------------------------------------------------------

def report_cpld(ssh):
    """Stage 09 — CPLD version, PSU present/pgood bits, LED register raw values."""
    sysfs = '/sys/bus/i2c/devices/1-0032'

    ver_raw, _, _ = ssh.run(f"cat {sysfs}/cpld_version 2>/dev/null || echo N/A")
    print(f"\n  CPLD version : {ver_raw.strip()}")

    psu_rows = []
    for slot in (1, 2):
        for attr, label in [('psu_present', 'present'), ('psu_power_good', 'pgood')]:
            val, _, rc = ssh.run(f"cat {sysfs}/{attr}{slot} 2>/dev/null")
            psu_rows.append((f"PSU{slot}", label, val.strip() if rc == 0 else "err"))
    _table(["PSU", "Signal", "Value"], psu_rows, title="PSU sysfs bits")

    led_rows = []
    for label, attr, reg in [("SYS1", 'led_sys1', '0x3e'), ("SYS2", 'led_sys2', '0x3f')]:
        val, _, rc = ssh.run(f"cat {sysfs}/{attr} 2>/dev/null")
        led_rows.append((label, reg, val.strip() if rc == 0 else "err"))
    _table(["LED", "Reg", "Raw"], led_rows, title="LED registers")


# ---------------------------------------------------------------------------
# Stage 10 — I2C daemon health
# ---------------------------------------------------------------------------

def report_daemon(ssh):
    """Stage 10 — i2c-poller timer state and /run/wedge100s/ cache health."""
    timer_state, _, _ = ssh.run(
        "systemctl is-active wedge100s-i2c-poller.timer 2>/dev/null || echo unknown"
    )
    print(f"\n  i2c-poller timer : {timer_state.strip()}")

    last_trigger, _, _ = ssh.run(
        "systemctl show wedge100s-i2c-poller.timer "
        "--property=LastTriggerUSec --value 2>/dev/null || echo unknown"
    )
    print(f"  last trigger     : {last_trigger.strip()}")

    age_out, _, _ = ssh.run(
        "find /run/wedge100s -name 'sfp_*_present' -printf '%T@\\n' 2>/dev/null "
        "| sort -n | head -1"
    )
    if age_out.strip():
        import time as _time
        try:
            oldest = float(age_out.strip())
            age_s = int(_time.time() - oldest)
            print(f"  oldest present   : {age_s}s ago")
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Stage 11 — Transceiver STATE_DB
# ---------------------------------------------------------------------------

def report_transceiver(ssh):
    """Stage 11 — TRANSCEIVER_INFO and TRANSCEIVER_DOM_SENSOR from STATE_DB."""
    info_out, _, _ = ssh.run(
        "redis-cli -n 6 KEYS 'TRANSCEIVER_INFO|*' 2>/dev/null | sort", timeout=20
    )
    ports = [l.split('|', 1)[1] for l in info_out.splitlines() if '|' in l]

    if not ports:
        print("\n  No transceiver info in STATE_DB")
        return

    rows = []
    for port in ports[:16]:
        vendor, _, _ = ssh.run(
            f"redis-cli -n 6 HGET 'TRANSCEIVER_INFO|{port}' vendor_name 2>/dev/null"
        )
        pn, _, _ = ssh.run(
            f"redis-cli -n 6 HGET 'TRANSCEIVER_INFO|{port}' vendor_part_number 2>/dev/null"
        )
        temp, _, _ = ssh.run(
            f"redis-cli -n 6 HGET 'TRANSCEIVER_DOM_SENSOR|{port}' temperature 2>/dev/null"
        )
        rows.append((port, vendor.strip() or "—", pn.strip() or "—", temp.strip() or "—"))

    _table(["Port", "Vendor", "Part Number", "Temp (°C)"], rows, title=f"Transceivers (first {len(rows)} of {len(ports)})")
    if len(ports) > 16:
        print(f"    … and {len(ports) - 16} more")


# ---------------------------------------------------------------------------
# Stage 12 — SFP Inventory
# ---------------------------------------------------------------------------

def _sfp_parse_pm(pm_text):
    """Parse 'show interfaces transceiver pm' output into a compact dict.

    Returns {'temp': str, 'voltage': str, 'lanes': [(rx_pwr, tx_bias, tx_pwr), ...]}
    All values may be 'N/A' for passive DAC cables.
    """
    temp    = re.search(r'Temperature:\s*(\S+)',         pm_text)
    voltage = re.search(r'Voltage:\s*(\S+)',              pm_text)
    lanes   = re.findall(r'^\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)', pm_text, re.MULTILINE)
    return {
        'temp':    temp.group(1)    if temp    else 'N/A',
        'voltage': voltage.group(1) if voltage else 'N/A',
        'lanes':   lanes,
    }


def report_sfp_inventory(ssh):
    """Stage 12 — SFP/QSFP inventory: vendor identity and PM data per physical port.

    For breakout configurations (4x25G, 2x50G), only the primary sub-port
    (alias ending /1) is shown — all sub-ports share one physical transceiver.
    """
    # --- collect all port aliases from CONFIG_DB ---
    keys_out, _, _ = ssh.run(
        "redis-cli -n 4 KEYS 'PORT|Ethernet*' 2>/dev/null | sort -V", timeout=20
    )
    alias_map = {}
    for key in keys_out.strip().splitlines():
        if '|' not in key:
            continue
        port = key.split('|', 1)[1].strip()
        alias_out, _, _ = ssh.run(
            f"redis-cli -n 4 HGET '{key}' alias 2>/dev/null", timeout=5
        )
        alias_map[port] = alias_out.strip()

    # --- presence ---
    presence_out, _, _ = ssh.run(
        "show interfaces transceiver presence 2>/dev/null", timeout=30
    )
    present = set()
    for line in presence_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+Present\b', line)
        if m:
            present.add(m.group(1))

    def _eth_key(name):
        m = re.search(r'(\d+)', name)
        return int(m.group(1)) if m else 0

    # physical primary ports = present AND alias ends with /1
    physical = [
        p for p in sorted(present, key=_eth_key)
        if alias_map.get(p, '').endswith('/1')
    ]

    if not physical:
        print("\n  No present physical ports found")
        return

    # --- per-port collection ---
    rows_id  = []   # for identity table
    rows_pm  = []   # for PM table
    for port in physical:
        alias = alias_map.get(port, '?')

        eeprom_out, _, _ = ssh.run(
            f"show interfaces transceiver eeprom {port} 2>/dev/null", timeout=30
        )
        pm_out, _, _ = ssh.run(
            f"show interfaces transceiver pm {port} 2>/dev/null", timeout=30
        )

        def _field(pattern, text):
            m = re.search(pattern, text)
            return m.group(1).strip() if m else '—'

        vendor = _field(r'Vendor Name:\s*(.+)',  eeprom_out)
        pn     = _field(r'Vendor PN:\s*(.+)',    eeprom_out)
        sn     = _field(r'Vendor SN:\s*(.+)',    eeprom_out)
        rows_id.append((port, alias, vendor, pn, sn))

        pm = _sfp_parse_pm(pm_out)
        rows_pm.append((port, alias, pm['temp'], pm['voltage'], pm['lanes']))

    _table(
        ["Port", "Alias", "Vendor", "Part Number", "Serial"],
        rows_id,
        title=f"SFP/QSFP Inventory ({len(physical)} physical ports)",
    )

    # PM: one row per lane so all bias/power values are visible
    pad = "  "
    print(f"\n{pad}Transceiver PM  (N/A = DAC cable or no DOM electronics):")
    headers = ["Port", "Alias", "Temp(C)", "Volt(V)", "Ln", "RxPwr(dBm)", "TxBias(mA)", "TxPwr(dBm)"]
    widths  = [14, 14, 8, 8, 4, 12, 12, 12]
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line    = "  ".join("-" * w for w in widths)
    print(f"{pad}{header_line}")
    print(f"{pad}{sep_line}")
    for port, alias, temp, voltage, lanes in rows_pm:
        if not lanes:
            cells = [port, alias, temp, voltage, "—", "", "", ""]
            print(f"{pad}" + "  ".join(str(cells[i]).ljust(widths[i]) for i in range(len(headers))))
            continue
        for i, (rx_pwr, tx_bias, tx_pwr) in enumerate(lanes):
            cells = [
                port  if i == 0 else "",
                alias if i == 0 else "",
                temp  if i == 0 else "",
                voltage if i == 0 else "",
                str(i + 1), rx_pwr, tx_bias, tx_pwr,
            ]
            print(f"{pad}" + "  ".join(str(cells[j]).ljust(widths[j]) for j in range(len(headers))))


# ---------------------------------------------------------------------------
# Stage 24 — Interface counters (link-up ports)
# ---------------------------------------------------------------------------

def report_counters(ssh):
    """Stage 24 — RX/TX packets and errors for link-up ports."""
    status_out, _, _ = ssh.run("show interfaces status 2>/dev/null", timeout=30)
    up_ports = set()
    for line in status_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+', line)
        if m and ' up ' in line:
            up_ports.add(m.group(1))

    if not up_ports:
        print("\n  No link-up ports")
        return

    ctr_out, _, _ = ssh.run("show interfaces counters 2>/dev/null", timeout=30)
    rows = []
    for line in ctr_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)', line)
        if m and m.group(1) in up_ports:
            rows.append((m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)))

    _table(["Port", "RX_OK", "RX_ERR", "TX_OK", "TX_ERR"], rows, title="Link-up port counters")


# ---------------------------------------------------------------------------
# Stage 14 — Breakout mode
# ---------------------------------------------------------------------------

def report_breakout(ssh):
    """Stage 14 — Active breakout mode per parent port from CONFIG_DB vs platform.json defaults."""
    _BREAKOUT_PARENTS = ["Ethernet0", "Ethernet64", "Ethernet80"]
    rows = []
    for parent in _BREAKOUT_PARENTS:
        # CONFIG_DB breakout mode
        mode_out, _, _ = ssh.run(
            f"redis-cli -n 4 HGET 'BREAKOUT_CFG|{parent}' brkout_mode 2>/dev/null"
        )
        mode = mode_out.strip() or "1x100G[40G]"
        # Count sub-ports in CONFIG_DB
        count_out, _, _ = ssh.run(
            f"redis-cli -n 4 KEYS 'PORT|{parent}*' 2>/dev/null | wc -l"
        )
        count = count_out.strip()
        rows.append((parent, mode, count))
    _table(["Parent Port", "Breakout Mode", "Sub-port Count"], rows, title="Breakout configuration")


# ---------------------------------------------------------------------------
# Stage 15 — Autoneg / FEC
# ---------------------------------------------------------------------------

def report_autoneg_fec(ssh):
    """Stage 15 — FEC mode and autoneg state for connected ports."""
    status_out, _, _ = ssh.run("show interfaces status 2>/dev/null", timeout=30)
    rows = []
    for line in status_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+[\d,]+\s+(\S+)\s+\d+\s+(\S+)\s+\S+\s+(\S+)', line)
        if m:
            port, speed, fec, oper = m.group(1), m.group(2), m.group(3), m.group(4)
            if oper == 'up':
                rows.append((port, speed, fec, oper))

    _table(["Port", "Speed", "FEC", "Oper"], rows, title="FEC and autoneg (link-up ports)")


# ---------------------------------------------------------------------------
# Stage 16 — PortChannel
# ---------------------------------------------------------------------------

def report_portchannel(ssh):
    """Stage 16 — PortChannel1 members, LACP state, VLAN membership."""
    pc_out, _, _ = ssh.run("show interfaces portchannel 2>/dev/null", timeout=20)
    print(f"\n  PortChannel state:\n")
    for line in pc_out.strip().splitlines()[:20]:
        print(f"    {line}")

    lacp_out, _, rc = ssh.run("teamdctl PortChannel1 state 2>/dev/null || echo N/A")
    if rc == 0 and lacp_out.strip() != "N/A":
        import json as _json
        try:
            state = _json.loads(lacp_out)
            ports = state.get("ports", {})
            rows = [(p, d.get("runner", {}).get("selected", "?"), d.get("link", {}).get("up", "?"))
                    for p, d in sorted(ports.items())]
            _table(["Member", "LACP Selected", "Link Up"], rows, title="LACP member state")
        except Exception:
            print(f"\n  teamdctl output: {lacp_out.strip()[:200]}")

    vlan_out, _, _ = ssh.run(
        "redis-cli -n 4 SMEMBERS 'VLAN_MEMBER|Vlan999' 2>/dev/null"
    )
    print(f"\n  Vlan999 members: {vlan_out.strip() or '(none)'}")


# ---------------------------------------------------------------------------
# Stage 19 — Platform CLI
# ---------------------------------------------------------------------------

def report_platform_cli(ssh):
    """Stage 19 — Base MAC, reboot cause, CPLD/BIOS version, watchdog status."""
    rows = []

    mac_out, _, _ = ssh.run("show platform syseeprom 2>/dev/null | grep 'Base MAC' || echo N/A")
    rows.append(("Base MAC", mac_out.strip()))

    reboot_out, _, _ = ssh.run("show reboot-cause 2>/dev/null || echo N/A")
    rows.append(("Reboot cause", reboot_out.strip()[:80]))

    cpld_out, _, _ = ssh.run(
        "cat /sys/bus/i2c/devices/1-0032/cpld_version 2>/dev/null || echo N/A"
    )
    rows.append(("CPLD version", cpld_out.strip()))

    bios_out, _, _ = ssh.run("sudo dmidecode -s bios-version 2>/dev/null || echo N/A")
    rows.append(("BIOS version", bios_out.strip()))

    wd_out, _, _ = ssh.run("show platform watchdog 2>/dev/null || echo N/A")
    rows.append(("Watchdog", wd_out.strip()[:80]))

    _table(["Item", "Value"], rows, title="Platform CLI summary")


# ---------------------------------------------------------------------------
# Stage 20 — Traffic counters
# ---------------------------------------------------------------------------

def report_traffic(ssh):
    """Stage 20 — TX/RX counter deltas for Ethernet16/32 from COUNTERS_DB."""
    _TRAFFIC_PORTS = ["Ethernet16", "Ethernet32"]

    name_map_out, _, _ = ssh.run(
        "redis-cli -n 2 HGETALL COUNTERS_PORT_NAME_MAP 2>/dev/null", timeout=15
    )
    name_map = {}
    tokens = name_map_out.split()
    for i in range(0, len(tokens) - 1, 2):
        name_map[tokens[i]] = tokens[i + 1]

    rows = []
    for port in _TRAFFIC_PORTS:
        oid = name_map.get(port)
        if not oid:
            rows.append((port, "OID not found", "—", "—", "—"))
            continue
        rx_out, _, _ = ssh.run(
            f"redis-cli -n 2 HGET COUNTERS:{oid} SAI_PORT_STAT_IF_IN_UCAST_PKTS 2>/dev/null"
        )
        tx_out, _, _ = ssh.run(
            f"redis-cli -n 2 HGET COUNTERS:{oid} SAI_PORT_STAT_IF_OUT_UCAST_PKTS 2>/dev/null"
        )
        rows.append((port, oid[:16] + "…", rx_out.strip() or "0", tx_out.strip() or "0", "snapshot"))

    _table(["Port", "OID (truncated)", "RX ucast pkts", "TX ucast pkts", "Note"],
           rows, title="Traffic counters (COUNTERS_DB snapshot)")
    print("\n  Note: run 'show interfaces counters' for human-readable deltas")


# ---------------------------------------------------------------------------
# Stage 21 — LP_MODE
# ---------------------------------------------------------------------------

def report_lpmode(ssh):
    """Stage 21 — LP_MODE state per installed SFP from /run/wedge100s/ cache."""
    rows = []
    for idx in range(32):
        present_raw, _, _ = ssh.run(
            f"cat /run/wedge100s/sfp_{idx}_present 2>/dev/null || echo -"
        )
        present = present_raw.strip()
        if present != "1":
            continue
        lp_raw, _, _ = ssh.run(
            f"cat /run/wedge100s/sfp_{idx}_lpmode 2>/dev/null || echo -"
        )
        lp = "asserted" if lp_raw.strip() == "1" else "deasserted"
        rows.append((f"port {idx}", "Yes", lp))

    if not rows:
        print("\n  No present ports with lpmode cache files")
        return
    _table(["Port", "Present", "LP_MODE"], rows, title="LP_MODE state")


# ---------------------------------------------------------------------------
# Stage 23 — Throughput
# ---------------------------------------------------------------------------

_TOPO_PATH_REL = os.path.join(os.path.dirname(__file__), '..', '..', 'tools', 'topology.json')
_TARGET_CFG_REL = os.path.join(os.path.dirname(__file__), '..', 'target.cfg')

_REPORT_IPERF_DURATION = 10  # seconds — brief spot-check for report mode

# (server_port, client_port, speed_label, threshold_gbps, round_label)
_THROUGHPUT_ROUNDS = [
    [
        ("Ethernet0",  "Ethernet80", "25G cross-QSFP", 20.0),
        ("Ethernet66", "Ethernet67", "10G same-QSFP",   8.0),
    ],
    [
        ("Ethernet80", "Ethernet81", "25G same-QSFP",  20.0),
        ("Ethernet66", "Ethernet0",  "10G×25G cross",   8.0),
    ],
]


def _report_iperf_pair(host_a, host_b, creds, duration):
    """Run iperf3: server on host_a bound to test_ip, client from host_b bound to its test_ip.

    Both endpoints bind to 10.0.10.x (-B) so traffic routes through switch VLAN 10.
    Returns bits_per_second (float) or raises on failure.
    """
    import paramiko, time as _time, json as _json

    def _connect(ip):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": ip, "username": creds["ssh_user"], "timeout": 10}
        if creds.get("key_file"):
            kw["key_filename"] = os.path.expanduser(creds["key_file"])
        c.connect(**kw)
        return c

    def _run(c, cmd, timeout=30):
        _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc  = stdout.channel.recv_exit_status()
        return out, err, rc

    srv = _connect(host_a["mgmt_ip"])
    cli = _connect(host_b["mgmt_ip"])
    try:
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        _run(srv, f"nohup iperf3 -s -1 -B {host_a['test_ip']} -D 2>/dev/null &", timeout=5)
        _time.sleep(1)
        out, err, rc = _run(
            cli,
            f"iperf3 -c {host_a['test_ip']} -B {host_b['test_ip']} -t {duration} --json",
            timeout=duration + 15,
        )
        if rc != 0:
            raise RuntimeError(f"iperf3 client failed: {err.strip()[:120]}")
        data = _json.loads(out)
        return data["end"]["sum_received"]["bits_per_second"]
    finally:
        _run(srv, "pkill -f 'iperf3 -s' 2>/dev/null || true")
        srv.close()
        cli.close()


def report_throughput(ssh):
    """Stage 23 — Live iperf3 throughput: 2 parallel rounds (host-to-host via VLAN 10)."""
    import json as _json
    import configparser as _cp
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Load topology
    try:
        with open(_TOPO_PATH_REL) as f:
            topo = _json.load(f)
    except Exception as exc:
        print(f"\n  Cannot load topology.json: {exc}")
        return
    host_map = {h["port"]: h for h in topo.get("hosts", [])}

    # Load SSH credentials for test hosts
    cfg = _cp.ConfigParser()
    cfg.read(_TARGET_CFG_REL)
    creds = {
        "ssh_user": cfg.get("hosts", "ssh_user", fallback="flax"),
        "key_file":  cfg.get("hosts", "key_file",  fallback="~/.ssh/id_rsa"),
    }

    all_rows = []

    for round_num, pairs in enumerate(_THROUGHPUT_ROUNDS, start=1):
        round_results = {}
        # Run both pairs in the round concurrently
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            for port_a, port_b, label, thresh in pairs:
                ha = host_map.get(port_a)
                hb = host_map.get(port_b)
                key = (port_a, port_b, label, thresh)
                if not ha or not hb:
                    round_results[key] = ("SKIP", None, "hosts not in topology")
                else:
                    futures[ex.submit(_report_iperf_pair, ha, hb, creds,
                                      _REPORT_IPERF_DURATION)] = key
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    bps = fut.result()
                    round_results[key] = ("OK", bps, "")
                except Exception as exc:
                    round_results[key] = ("ERR", None, str(exc)[:60])

        for port_a, port_b, label, thresh in pairs:
            key = (port_a, port_b, label, thresh)
            status_code, bps, note = round_results.get(key, ("ERR", None, "future missing"))
            if status_code == "SKIP":
                all_rows.append((f"R{round_num}: {port_a}↔{port_b}", label, "—", "SKIP", note))
            elif status_code == "ERR":
                all_rows.append((f"R{round_num}: {port_a}↔{port_b}", label, "—", "ERROR", note))
            else:
                gbps   = bps / 1e9
                result = "PASS" if gbps >= thresh else "FAIL"
                all_rows.append((
                    f"R{round_num}: {port_a}↔{port_b}", label,
                    f"{gbps:.2f} Gbps", result,
                    f"threshold {thresh:.0f} Gbps",
                ))

    _table(
        ["Pair", "Link", "Measured", "Result", "Note"],
        all_rows,
        title=f"Host-to-host throughput ({_REPORT_IPERF_DURATION}s iperf3, 2 rounds × 2 concurrent)",
    )


# ---------------------------------------------------------------------------
# Registry: stage name → reporter function
# ---------------------------------------------------------------------------

REPORTERS = {
    "stage_01_eeprom":      report_eeprom,
    "stage_02_system":      report_system,
    "stage_03_platform":    report_platform,
    "stage_04_thermal":     report_thermal,
    "stage_05_fan":         report_fan,
    "stage_06_psu":         report_psu,
    "stage_07_qsfp":        report_qsfp,
    "stage_08_led":         report_led,
    "stage_09_cpld":        report_cpld,
    "stage_10_daemon":      report_daemon,
    "stage_11_transceiver":    report_transceiver,
    "stage_12_sfp_inventory":  report_sfp_inventory,
    "stage_13_link":           report_link,
    "stage_14_breakout":    report_breakout,
    "stage_15_autoneg_fec": report_autoneg_fec,
    "stage_16_portchannel": report_portchannel,
    "stage_19_platform_cli": report_platform_cli,
    "stage_20_traffic":     report_traffic,
    "stage_21_lpmode":      report_lpmode,
    "stage_23_throughput":  report_throughput,
    "stage_24_counters":    report_counters,
}
