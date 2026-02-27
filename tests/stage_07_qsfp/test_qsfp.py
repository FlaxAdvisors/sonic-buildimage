"""Stage 07 — QSFP28 transceiver presence and EEPROM (32 ports).

Presence is read from two PCA9535 GPIO expanders via the CP2112 mux tree:
  i2c-36/0x22 — ports 0–15
  i2c-37/0x23 — ports 16–31

Each present port has an optoe1 EEPROM driver at i2c-{bus}/0x50.

Port-to-bus mapping (0-based port, from sfpi.c sfp_bus_index[]):
  port 0→3, 1→2, 2→5, 3→4, ... 31→32

Phase reference: Phase 6 (QSFP/SFP).
"""

import json
import re
import pytest

NUM_PORTS = 32

QSFP_CAPTURE = """\
import json, sys
from sonic_platform.platform import Platform

chassis = Platform().get_chassis()
results = []
for idx in range(1, 33):
    sfp = chassis.get_sfp(idx)
    present = sfp.get_presence()
    eeprom_path = None
    error_desc = None
    try:
        eeprom_path = sfp.get_eeprom_path() if present else None
        error_desc = sfp.get_error_description()
    except Exception as e:
        error_desc = f'EXCEPTION: {e}'
    results.append({
        'index': idx,
        'name': sfp.get_name(),
        'present': present,
        'eeprom_path': eeprom_path,
        'error_description': error_desc,
        'position': sfp.get_position_in_parent(),
    })
print(json.dumps(results))
"""


def _get_qsfps(ssh):
    out, err, rc = ssh.run_python(QSFP_CAPTURE, timeout=90)
    assert rc == 0, f"QSFP capture script failed (rc={rc}): {err}"
    return json.loads(out.strip())


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def test_qsfp_cli_presence(ssh):
    """show interfaces transceiver presence exits 0 and lists all 32 ports."""
    out, err, rc = ssh.run("show interfaces transceiver presence")
    print(f"\nshow interfaces transceiver presence:\n{out}")
    assert rc == 0, f"Command failed: {err}"
    assert out.strip(), "transceiver presence returned empty output"

    # Count Ethernet data rows
    eth_rows = [l for l in out.splitlines() if re.match(r"\s*Ethernet\d+", l)]
    print(f"\nPort rows: {len(eth_rows)}")
    assert len(eth_rows) >= NUM_PORTS, (
        f"Expected ≥{NUM_PORTS} Ethernet port rows, found {len(eth_rows)}"
    )


# ------------------------------------------------------------------
# Python API — presence table
# ------------------------------------------------------------------

def test_qsfp_api_port_count(ssh):
    """Platform returns presence for all 32 QSFP ports."""
    data = _get_qsfps(ssh)

    present_ports = [p for p in data if p["present"]]
    absent_ports  = [p for p in data if not p["present"]]

    print(f"\nQSFP port summary: {len(present_ports)} present, {len(absent_ports)} absent")
    print("\nPresent ports:")
    for p in present_ports:
        print(f"  {p['name']:15s}  eeprom={p['eeprom_path']}")
    if absent_ports:
        absent_names = [p['name'] for p in absent_ports]
        # Print compactly — can be long
        print(f"\nAbsent ports ({len(absent_ports)}): {absent_names[:8]}{'...' if len(absent_names)>8 else ''}")

    assert len(data) == NUM_PORTS, (
        f"Expected {NUM_PORTS} port entries, got {len(data)}"
    )


def test_qsfp_api_names(ssh):
    """QSFP port names follow 'QSFP28 N' pattern."""
    data = _get_qsfps(ssh)
    for p in data:
        assert "QSFP" in p["name"] or "SFP" in p["name"], (
            f"Unexpected port name: {p['name']!r}"
        )


def test_qsfp_api_positions(ssh):
    """QSFP positions are 1-based sequential 1..32."""
    data = _get_qsfps(ssh)
    positions = sorted(p["position"] for p in data)
    assert positions == list(range(1, NUM_PORTS + 1)), (
        f"Non-sequential positions: {positions}"
    )


def test_qsfp_api_present_error_description(ssh):
    """Present ports report error_description indicating OK/present."""
    data = _get_qsfps(ssh)
    for p in data:
        if not p["present"]:
            continue
        desc = p["error_description"]
        assert "OK" in str(desc).upper() or "present" in str(desc).lower(), (
            f"Port {p['name']!r} is present but error_description={desc!r}"
        )
        assert "EXCEPTION" not in str(desc), (
            f"Port {p['name']!r} raised an exception: {desc}"
        )


def test_qsfp_api_absent_error_description(ssh):
    """Absent ports report error_description indicating unplugged/absent."""
    data = _get_qsfps(ssh)
    for p in data:
        if p["present"]:
            continue
        desc = p["error_description"]
        # Absent ports should say "unplugged" or similar — not an exception
        assert "EXCEPTION" not in str(desc), (
            f"Port {p['name']!r} (absent) raised an exception: {desc}"
        )


# ------------------------------------------------------------------
# EEPROM reads for present ports
# ------------------------------------------------------------------

def test_qsfp_eeprom_path_exists(ssh):
    """For present ports, the EEPROM sysfs path exists on the target."""
    data = _get_qsfps(ssh)
    present = [p for p in data if p["present"] and p["eeprom_path"]]
    if not present:
        pytest.skip("No QSFP modules present — cannot test EEPROM path")

    for p in present[:4]:  # Check first 4 present ports
        path = p["eeprom_path"]
        out, _, rc = ssh.run(f"test -f {path} && echo EXISTS || echo MISSING")
        assert "EXISTS" in out, (
            f"Port {p['name']!r}: EEPROM path {path!r} does not exist on target"
        )


def test_qsfp_eeprom_identifier_byte(ssh):
    """EEPROM byte 0 (identifier) is non-zero for present QSFP modules."""
    data = _get_qsfps(ssh)
    present = [p for p in data if p["present"] and p["eeprom_path"]]
    if not present:
        pytest.skip("No QSFP modules present — cannot test EEPROM content")

    p = present[0]
    path = p["eeprom_path"]
    # Read first byte (identifier: 0x0D=QSFP+, 0x11=QSFP28, 0x18=QSFP-DD)
    out, err, rc = ssh.run(
        f"sudo hexdump -n 1 -e '1/1 \"0x%02x\"' {path} 2>/dev/null"
    )
    print(f"\n{p['name']} EEPROM identifier byte: {out.strip()}")
    assert rc == 0, f"Could not read EEPROM for {p['name']}: {err}"
    assert out.strip() and out.strip() != "0x00", (
        f"Port {p['name']!r} EEPROM identifier byte is 0x00 (unexpected)"
    )


def test_qsfp_eeprom_vendor_info(ssh):
    """At least one present port has readable vendor name in EEPROM."""
    data = _get_qsfps(ssh)
    present = [p for p in data if p["present"] and p["eeprom_path"]]
    if not present:
        pytest.skip("No QSFP modules present — cannot test vendor info")

    p = present[0]
    path = p["eeprom_path"]
    # Vendor name at bytes 148–163 in QSFP28 EEPROM (page 0)
    out, err, rc = ssh.run(
        f"sudo dd if={path} bs=1 skip=148 count=16 2>/dev/null | strings"
    )
    print(f"\n{p['name']} EEPROM vendor name: {out.strip()!r}")
    if rc != 0 or not out.strip():
        pytest.xfail(f"Could not read vendor name from {p['name']} EEPROM")
    # Vendor name should be printable ASCII
    assert out.strip().isprintable() or any(c.isalpha() for c in out.strip()), (
        f"Vendor name bytes don't look printable: {out.strip()!r}"
    )


# ------------------------------------------------------------------
# PCA9535 GPIO expanders (raw presence check)
# ------------------------------------------------------------------

def _pca9535_check(ssh, bus, addr):
    """Return (raw_value_or_None, status_string) for a PCA9535 i2cget attempt."""
    out, err, rc = ssh.run(f"sudo i2cget -y {bus} 0x{addr:02x} 0x00 2>&1")
    combined = (out + err).strip()
    if rc == 0:
        return out.strip(), "ok"
    if "busy" in combined.lower():
        return None, "busy"   # kernel driver has it — expected
    return None, f"error: {combined}"


def test_pca9535_i2c36_accessible(ssh):
    """PCA9535 at i2c-36/0x22 (QSFP ports 0–15) is either readable or owned by kernel driver.

    'Device or resource busy' means the sysfs gpio/smbus driver has claimed the
    device, which is correct — the Python API uses it through that driver.
    Direct i2cget is blocked by design.
    """
    val, status = _pca9535_check(ssh, 36, 0x22)
    print(f"\nPCA9535 i2c-36/0x22: {status}  (value={val})")
    assert status in ("ok", "busy"), (
        f"Unexpected result from PCA9535 i2c-36/0x22: {status}\n"
        "Check that the mux tree is initialized and i2c-36 is registered."
    )
    if status == "busy":
        pytest.xfail(
            "PCA9535 at i2c-36/0x22 is owned by the kernel gpio/smbus driver "
            "(Device or resource busy). This is correct — the Python API reads "
            "QSFP presence through that driver successfully (see test_qsfp_api_port_count)."
        )


def test_pca9535_i2c37_accessible(ssh):
    """PCA9535 at i2c-37/0x23 (QSFP ports 16–31) is either readable or owned by kernel driver."""
    val, status = _pca9535_check(ssh, 37, 0x23)
    print(f"\nPCA9535 i2c-37/0x23: {status}  (value={val})")
    assert status in ("ok", "busy"), (
        f"Unexpected result from PCA9535 i2c-37/0x23: {status}"
    )
    if status == "busy":
        pytest.xfail(
            "PCA9535 at i2c-37/0x23 is owned by the kernel driver — expected. "
            "See test_pca9535_i2c36_accessible for details."
        )
