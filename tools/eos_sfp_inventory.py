#!/usr/bin/env python3
"""EOS peer SFP inventory and PM diagnostic tool.

Connects to the Arista EOS peer switch (default 192.168.88.14) and collects:
  - Transceiver slot inventory: port, manufacturer, model, serial, rev
  - Per-lane PM data: temp, voltage, bias, Rx/Tx power

Optionally cross-references the SONiC switch inventory (default 192.168.88.12)
to identify which DAC cable connects each EOS port to a SONiC port, using the
serial-number prefix convention (same cable = same SN prefix, -1 and -2 suffixes).

Usage:
    python3 tools/eos_sfp_inventory.py
    python3 tools/eos_sfp_inventory.py --no-sonic
    python3 tools/eos_sfp_inventory.py --eos-host 192.168.88.14 --sonic-host 192.168.88.12

Access:
    EOS  : admin / 0penSesame  (hardcoded; use --eos-password to override)
    SONiC: admin / key from tests/target.cfg  (or --sonic-password)
"""

import argparse
import os
import re
import sys

try:
    import paramiko
except ImportError:
    sys.exit("paramiko required: pip install paramiko")


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _connect(host, username, password, timeout=15):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=username, password=password,
              timeout=timeout, allow_agent=False, look_for_keys=False)
    return c


def _run(client, command, timeout=30):
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err


# ---------------------------------------------------------------------------
# EOS parsers
# ---------------------------------------------------------------------------

def _parse_eos_inventory(text):
    """Parse the transceiver section of 'show inventory'.

    Returns list of dicts with keys: port(int), manufacturer, model, serial, rev.
    Skips 'Not Present' slots.
    """
    # Find the transceiver table — starts after "switched transceiver slots" header
    in_table = False
    ports = []
    for line in text.splitlines():
        if 'transceiver' in line.lower() and 'slot' in line.lower():
            in_table = True
            continue
        if not in_table:
            continue
        # Table rows: "  N    Manufacturer     Model        Serial       Rev"
        m = re.match(r'^\s+(\d+)\s+(\S.+?)\s{2,}(\S.+?)\s{2,}(\S+)\s+(\S*)', line)
        if m:
            mfr = m.group(2).strip()
            if 'Not Present' in mfr:
                continue
            ports.append({
                'port':         int(m.group(1)),
                'manufacturer': mfr,
                'model':        m.group(3).strip(),
                'serial':       m.group(4).strip(),
                'rev':          m.group(5).strip(),
            })
        elif in_table and line.strip() and not line[0].isspace():
            # Blank section header — stop at next top-level section
            if ports:
                break
    return ports


def _parse_eos_pm(text):
    """Parse 'show interfaces transceiver' output.

    Returns dict: {(port_num, lane): {'temp', 'voltage', 'bias', 'tx_pwr', 'rx_pwr'}}
    Only entries where at least one value is not N/A are included.
    Port/lane derived from EtN/L format.
    """
    pm = {}
    for line in text.splitlines():
        # Et22/1     27.00      3.22      6.01     -0.24     0.32      0:00:00 ago
        m = re.match(
            r'^\s*Et(\d+)/(\d+)\s+'
            r'(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)',
            line
        )
        if not m:
            continue
        port, lane = int(m.group(1)), int(m.group(2))
        temp, volt, bias, tx_pwr, rx_pwr = (
            m.group(3), m.group(4), m.group(5), m.group(6), m.group(7)
        )
        if all(v == 'N/A' for v in (temp, volt, bias, tx_pwr, rx_pwr)):
            continue
        pm[(port, lane)] = {
            'temp':    temp,
            'voltage': volt,
            'bias':    bias,
            'tx_pwr':  tx_pwr,
            'rx_pwr':  rx_pwr,
        }
    return pm


# ---------------------------------------------------------------------------
# SONiC parsers
# ---------------------------------------------------------------------------

def _get_sonic_inventory(client):
    """Collect SFP inventory from SONiC via redis and show interfaces transceiver eeprom.

    Returns list of dicts: {port, alias, manufacturer, model, serial}
    Only physical primary ports (alias /1) that have a transceiver installed.
    """
    # Get all port aliases
    out, _ = _run(client, "redis-cli -n 4 KEYS 'PORT|Ethernet*' 2>/dev/null")
    alias_map = {}
    for key in out.strip().splitlines():
        if '|' not in key:
            continue
        port = key.split('|', 1)[1].strip()
        a_out, _ = _run(client, f"redis-cli -n 4 HGET '{key}' alias 2>/dev/null")
        alias_map[port] = a_out.strip()

    # Presence
    pres_out, _ = _run(client, "show interfaces transceiver presence 2>/dev/null")
    present = set()
    for line in pres_out.splitlines():
        m = re.match(r'\s*(Ethernet\d+)\s+Present\b', line)
        if m:
            present.add(m.group(1))

    def _eth_key(name):
        m = re.search(r'(\d+)', name)
        return int(m.group(1)) if m else 0

    physical = [
        p for p in sorted(present, key=_eth_key)
        if alias_map.get(p, '').endswith('/1')
    ]

    inventory = []
    for port in physical:
        alias = alias_map.get(port, '')
        eeprom, _ = _run(client, f"show interfaces transceiver eeprom {port} 2>/dev/null")
        pm_raw, _ = _run(client, f"show interfaces transceiver pm {port} 2>/dev/null")

        def _f(pattern):
            m = re.search(pattern, eeprom)
            return m.group(1).strip() if m else ''

        pm_lanes = []
        for m in re.finditer(r'^\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)', pm_raw, re.MULTILINE):
            pm_lanes.append({
                'lane':    int(m.group(1)),
                'rx_pwr':  m.group(2),
                'tx_bias': m.group(3),
                'tx_pwr':  m.group(4),
            })
        pm_temp    = re.search(r'Temperature:\s*(\S+)', pm_raw)
        pm_voltage = re.search(r'Voltage:\s*(\S+)',     pm_raw)

        inventory.append({
            'port':         port,
            'alias':        alias,
            'manufacturer': _f(r'Vendor Name:\s*(.+)'),
            'model':        _f(r'Vendor PN:\s*(.+)'),
            'serial':       _f(r'Vendor SN:\s*(.+)'),
            'pm_lanes':     pm_lanes,
            'pm_temp':      pm_temp.group(1)    if pm_temp    else 'N/A',
            'pm_voltage':   pm_voltage.group(1) if pm_voltage else 'N/A',
        })
    return inventory


# ---------------------------------------------------------------------------
# Serial-number cross-reference
# ---------------------------------------------------------------------------

def _sn_prefix(serial):
    """Strip trailing -N suffix used to mark two ends of the same DAC cable.

    'F2032955533-1' → 'F2032955533'
    'MT1703VS02617' → 'MT1703VS02617'  (no suffix — active optic)
    """
    return re.sub(r'-\d+$', '', serial)


def _build_cross_ref(eos_ports, sonic_inv):
    """Return dict mapping EOS port number → SONiC port name (or None).

    Matches on serial-number prefix.  DAC cables show the same SN prefix with
    -1 / -2 suffixes on each end.  Active optics (SR4, LR4) have unrelated
    serials and will not cross-reference.
    """
    sonic_by_prefix = {}
    for s in sonic_inv:
        pfx = _sn_prefix(s['serial'])
        if pfx:
            sonic_by_prefix[pfx] = s

    xref = {}
    for e in eos_ports:
        pfx = _sn_prefix(e['serial'])
        match = sonic_by_prefix.get(pfx)
        xref[e['port']] = match['port'] if match else None
    return xref


# ---------------------------------------------------------------------------
# Table printer
# ---------------------------------------------------------------------------

def _table(headers, rows, title=None):
    pad = "  "
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eos-host",      default="192.168.88.14")
    ap.add_argument("--eos-user",      default="admin")
    ap.add_argument("--eos-password",  default="0penSesame")
    ap.add_argument("--sonic-host",    default="192.168.88.12")
    ap.add_argument("--sonic-user",    default="admin")
    ap.add_argument("--sonic-password",default=None,
                    help="SONiC password (reads tests/target.cfg if omitted)")
    ap.add_argument("--no-sonic",      action="store_true",
                    help="Skip SONiC cross-reference")
    args = ap.parse_args()

    # Resolve SONiC password from target.cfg if not given
    sonic_password = args.sonic_password
    if not sonic_password and not args.no_sonic:
        cfg = os.path.join(os.path.dirname(__file__), "..", "tests", "target.cfg")
        if os.path.exists(cfg):
            import configparser
            cp = configparser.ConfigParser()
            cp.read(cfg)
            try:
                sonic_password = cp["target"]["password"]
            except KeyError:
                pass

    # ── EOS connection ──────────────────────────────────────────────
    print(f"Connecting to EOS peer ({args.eos_host}) ...", flush=True)
    try:
        eos = _connect(args.eos_host, args.eos_user, args.eos_password)
    except Exception as e:
        sys.exit(f"EOS connection failed: {e}")

    inv_raw, _ = _run(eos, "show inventory")
    pm_raw,  _ = _run(eos, "show interfaces transceiver")
    eos.close()

    eos_ports = _parse_eos_inventory(inv_raw)
    eos_pm    = _parse_eos_pm(pm_raw)

    # ── SONiC connection (optional) ─────────────────────────────────
    sonic_inv = []
    if not args.no_sonic:
        print(f"Connecting to SONiC switch ({args.sonic_host}) ...", flush=True)
        try:
            sonic = _connect(args.sonic_host, args.sonic_user, sonic_password or "")
            sonic_inv = _get_sonic_inventory(sonic)
            sonic.close()
        except Exception as e:
            print(f"  [!] SONiC connection failed: {e} — skipping cross-reference")

    xref = _build_cross_ref(eos_ports, sonic_inv) if sonic_inv else {}

    # ── Inventory table ─────────────────────────────────────────────
    print(f"\n{'='*64}")
    print(f"  EOS Transceiver Inventory  ({args.eos_host})")
    print(f"{'='*64}")

    headers_inv = ["Slot", "Manufacturer", "Model", "Serial", "Rev"]
    if xref:
        headers_inv.append("SONiC Port")

    rows_inv = []
    for e in sorted(eos_ports, key=lambda x: x['port']):
        row = [e['port'], e['manufacturer'], e['model'], e['serial'], e['rev']]
        if xref:
            row.append(xref.get(e['port']) or "—")
        rows_inv.append(tuple(row))

    _table(headers_inv, rows_inv, title=f"{len(eos_ports)} installed transceivers")

    # ── PM table: only ports with live values ───────────────────────
    live_ports = sorted({p for p, _ in eos_pm.keys()})
    if live_ports:
        print(f"\n{'='*64}")
        print(f"  EOS Transceiver PM  (active optics only — DAC cables omitted)")
        print(f"{'='*64}")

        # Find max lanes for these ports
        headers_pm = ["Slot", "EOS Port", "Ln", "Temp(C)", "Volt(V)",
                       "Bias(mA)", "TxPwr(dBm)", "RxPwr(dBm)"]
        if xref:
            headers_pm.insert(2, "SONiC Port")

        # Find slot info for each live port
        slot_by_port = {e['port']: e for e in eos_ports}

        rows_pm = []
        for port_num in live_ports:
            # Collect all lanes for this port
            lanes = sorted(
                [(ln, eos_pm[(port_num, ln)]) for ln in range(1, 5)
                 if (port_num, ln) in eos_pm],
                key=lambda x: x[0]
            )
            slot = slot_by_port.get(port_num)
            eos_iface = f"Et{port_num}/1"

            for i, (lane_num, pm) in enumerate(lanes):
                slot_col  = str(port_num) if i == 0 else ""
                iface_col = eos_iface     if i == 0 else ""
                row = [slot_col, iface_col, str(lane_num),
                       pm['temp'], pm['voltage'], pm['bias'],
                       pm['tx_pwr'], pm['rx_pwr']]
                if xref:
                    sonic_port = xref.get(port_num, "—") or "—"
                    row.insert(2, sonic_port if i == 0 else "")
                rows_pm.append(tuple(row))

        _table(headers_pm, rows_pm,
               title=f"{len(live_ports)} ports with live PM data")
    else:
        print("\n  No live PM data — all installed transceivers are passive DAC cables.")

    # ── SONiC inventory for reference ──────────────────────────────
    if sonic_inv:
        print(f"\n{'='*64}")
        print(f"  SONiC SFP Inventory  ({args.sonic_host})  [for cross-reference]")
        print(f"{'='*64}")
        rows_sonic = [
            (e['port'], e['alias'], e['manufacturer'], e['model'], e['serial'])
            for e in sonic_inv
        ]
        _table(["Port", "Alias", "Manufacturer", "Model", "Serial"], rows_sonic,
               title=f"{len(sonic_inv)} physical ports with transceivers")

        # SONiC PM table — ports with any non-N/A lane values
        sonic_live = [e for e in sonic_inv if any(
            ln['rx_pwr'] != 'N/A' or ln['tx_bias'] != 'N/A' or ln['tx_pwr'] != 'N/A'
            for ln in e['pm_lanes']
        )]
        if sonic_live:
            print(f"\n{'='*64}")
            print(f"  SONiC Transceiver PM  ({args.sonic_host})  "
                  f"(active optics only — DAC cables omitted)")
            print(f"{'='*64}")
            pad = "  "
            headers_spm = ["Port", "Alias", "Temp(C)", "Volt(V)", "Ln",
                           "RxPwr(dBm)", "TxBias(mA)", "TxPwr(dBm)"]
            widths_spm  = [14, 14, 8, 8, 4, 12, 12, 12]
            print(f"\n{pad}  {len(sonic_live)} ports with live PM data:")
            print(f"{pad}  " + "  ".join(h.ljust(widths_spm[i])
                                         for i, h in enumerate(headers_spm)))
            print(f"{pad}  " + "  ".join("-" * w for w in widths_spm))
            for e in sonic_live:
                for i, ln in enumerate(e['pm_lanes']):
                    cells = [
                        e['port']      if i == 0 else "",
                        e['alias']     if i == 0 else "",
                        e['pm_temp']   if i == 0 else "",
                        e['pm_voltage'] if i == 0 else "",
                        str(ln['lane']), ln['rx_pwr'], ln['tx_bias'], ln['tx_pwr'],
                    ]
                    print(f"{pad}  " + "  ".join(
                        str(cells[j]).ljust(widths_spm[j]) for j in range(len(headers_spm))
                    ))

        # Cable topology: ports that cross-reference
        cable_rows = []
        for e in eos_ports:
            sonic_match = next(
                (s for s in sonic_inv
                 if _sn_prefix(s['serial']) == _sn_prefix(e['serial']) and e['serial']),
                None
            )
            if sonic_match:
                cable_rows.append((
                    f"Et{e['port']}/1", e['model'], e['serial'],
                    sonic_match['port'], sonic_match['alias'], sonic_match['serial'],
                ))
        if cable_rows:
            _table(
                ["EOS Port", "Model", "EOS Serial", "SONiC Port", "SONiC Alias", "SONiC Serial"],
                cable_rows,
                title="DAC cable topology  (matched by serial-number prefix)",
            )


if __name__ == "__main__":
    main()
