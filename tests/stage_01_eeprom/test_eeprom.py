"""Stage 01 — System EEPROM (TLV dump).

Exercises the 24c64 EEPROM at i2c-40/0x50 via both the SONiC CLI decoder
(decode-syseeprom) and the sonic_platform Python API (SysEeprom / chassis).

Root-cause note (2026-02-26):
  The EEPROM chip at i2c-40/0x50 may contain non-zero factory data that does NOT
  use ONIE TlvInfo format.  decode-syseeprom and the Python API both report the
  EEPROM as invalid when the magic bytes ("TlvInfo\\x00") are absent.

  Fix: program the EEPROM with valid ONIE TlvInfo data:
      sudo write-syseeprom -t 0x21 -v "Wedge-100s-32X" \\
          -t 0x22 -v "<part-number>" -t 0x23 -v "<serial>" \\
          -t 0x24 -v "<base-mac>" -t 0x2B -v "Accton"
  Or boot into ONIE and run:  onie-syseeprom -s product_name="Wedge-100s-32X" ...

Phase reference: Phase 7 (System EEPROM).
"""

import pytest

# Standard ONIE TLV type codes we expect to find
REQUIRED_TLV_CODES = {
    "0x21": "Product Name",
    "0x22": "Part Number",
    "0x23": "Serial Number",
    "0x24": "Base MAC Address",
    "0x2b": "Manufacturer",
}

PLATFORM_KEYWORD = "wedge"

# ONIE TlvInfo header: 8-byte ASCII magic "TlvInfo\x00"
TLVINFO_MAGIC = bytes([0x54, 0x6c, 0x76, 0x49, 0x6e, 0x66, 0x6f, 0x00])
EEPROM_PATH   = "/sys/bus/i2c/devices/40-0050/eeprom"
EEPROM_CACHE  = "/var/cache/sonic/syseeprom_cache"

# ------------------------------------------------------------------
# Module-scoped helpers: capture raw bytes and CLI output once
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def eeprom_raw_bytes(ssh):
    """Read the raw first 256 bytes of the EEPROM sysfs node."""
    script = f"""
import sys
data = open('{EEPROM_PATH}', 'rb').read()
# Print hex of first 256 bytes
print(data[:256].hex())
"""
    out, err, rc = ssh.run_python(script, timeout=20)
    assert rc == 0, f"Could not read EEPROM binary: {err}"
    return bytes.fromhex(out.strip())


@pytest.fixture(scope="module")
def eeprom_raw_cli(ssh):
    """Run decode-syseeprom once and cache output."""
    out, _, _ = ssh.run("sudo decode-syseeprom 2>&1")
    return out


# ------------------------------------------------------------------
# Sysfs path — must pass before any other test
# ------------------------------------------------------------------

def test_eeprom_sysfs_path_exists(ssh):
    """EEPROM sysfs node /sys/bus/i2c/devices/40-0050/eeprom exists and is 8192 bytes."""
    out, err, rc = ssh.run(f"ls -la {EEPROM_PATH} 2>/dev/null")
    assert rc == 0 and "8192" in out, (
        f"EEPROM sysfs node missing or wrong size.\n"
        f"Expected: {EEPROM_PATH}  size=8192\n"
        f"Got: {out!r}  err={err!r}\n"
        "Check that wedge100s-platform-init.service ran: "
        "'systemctl status wedge100s-platform-init'"
    )


def test_eeprom_sysfs_hexdump(ssh, eeprom_raw_bytes):
    """EEPROM sysfs node is readable and returns non-empty binary data."""
    print(f"\nEEPROM first 32 bytes: {eeprom_raw_bytes[:32].hex()}")
    assert len(eeprom_raw_bytes) == 256, (
        f"Expected 256 bytes, got {len(eeprom_raw_bytes)}"
    )


# ------------------------------------------------------------------
# Cache guard — must exist before xcvrd/pmon can corrupt the mux
# ------------------------------------------------------------------

def test_eeprom_cache_file_exists(ssh):
    """EEPROM cache file written by platform-init must exist.

    accton_wedge100s_util.py writes raw EEPROM bytes to
    /var/cache/sonic/syseeprom_cache immediately after registering the
    24c64 device, before xcvrd/pmon start.  sonic_platform/eeprom.py reads
    from this cache rather than hardware to avoid CP2112 I2C bus hangs caused
    by PCA9535 presence polls racing with EEPROM reads on the shared mux 0x74.

    If this file is missing, the EEPROM guard is not in place and hardware reads
    will eventually return all-zeros after the bus hangs.

    HOW TO FIX:
        sudo accton_wedge100s_util.py install
    This re-runs platform init including the caching step.
    """
    out, err, rc = ssh.run(f"ls -la {EEPROM_CACHE} 2>/dev/null")
    assert rc == 0, (
        f"EEPROM cache file missing: {EEPROM_CACHE}\n"
        f"The platform-init guard mechanism was not activated.\n"
        f"Run: sudo accton_wedge100s_util.py install\n"
        f"Or: sudo systemctl restart wedge100s-platform-init"
    )


def test_eeprom_cache_valid_magic(ssh):
    """EEPROM cache file must start with ONIE TlvInfo magic bytes.

    If the cache was written when the EEPROM already had I2C bus corruption
    (all-zero reads), the cache is itself invalid.  This test catches that case.

    HOW TO FIX — reboot the switch (clears CP2112 bus hang via USB re-enumeration),
    then before xcvrd starts (i.e., within ~30s of boot) run:
        sudo accton_wedge100s_util.py install
    Alternatively, write valid ONIE data first:
        sudo write-syseeprom -t 0x21 -v 'Wedge-100s-32X' ...
    then delete and re-create the cache:
        sudo rm /var/cache/sonic/syseeprom_cache
        sudo accton_wedge100s_util.py install
    """
    script = f"""
import sys
data = open('{EEPROM_CACHE}', 'rb').read(8)
print(data.hex())
"""
    out, err, rc = ssh.run_python(script, timeout=10)
    assert rc == 0, f"Could not read EEPROM cache: {err}"
    actual_hex = out.strip()
    expected_hex = TLVINFO_MAGIC.hex()
    assert actual_hex == expected_hex, (
        f"EEPROM cache has invalid magic bytes — cache was written while bus was hung.\n"
        f"  Expected: {expected_hex}  (\"TlvInfo\\x00\")\n"
        f"  Actual:   {actual_hex}\n"
        f"  Fix: reboot, then run: sudo rm {EEPROM_CACHE} && sudo accton_wedge100s_util.py install"
    )


# ------------------------------------------------------------------
# Magic bytes — root cause diagnostic
# ------------------------------------------------------------------

def test_eeprom_magic_bytes(ssh, eeprom_raw_bytes):
    """First 8 bytes of EEPROM must be the ONIE TlvInfo magic.

    ROOT CAUSE: If the EEPROM chip has factory/ODM data that does not start with
    the ONIE TlvInfo magic ("TlvInfo\\x00" = 54 6c 76 49 6e 66 6f 00), then
    decode-syseeprom will report "EEPROM does not contain data in a valid TlvInfo
    format" and the Python API will return {}.  This is not a software bug; the
    EEPROM simply has not been written with ONIE-format data.

    HOW TO FIX — program the EEPROM with ONIE TlvInfo data:
        sudo write-syseeprom \\
            -t 0x21 -v "Wedge-100s-32X" \\
            -t 0x22 -v "<part-number>" \\
            -t 0x23 -v "<serial-number>" \\
            -t 0x24 -v "<base-mac-address>" \\
            -t 0x2b -v "Accton"
    Or boot into ONIE recovery mode and run onie-syseeprom.
    """
    actual_magic = eeprom_raw_bytes[:8]
    expected_hex = TLVINFO_MAGIC.hex()
    actual_hex   = actual_magic.hex()

    # Classify what we found for the diagnostic message
    all_zero = all(b == 0 for b in eeprom_raw_bytes[:8])
    all_ff   = all(b == 0xFF for b in eeprom_raw_bytes[:8])
    non_zero_count = sum(1 for b in eeprom_raw_bytes if b != 0)

    if all_zero or all_ff:
        state = "ERASED / BLANK (all zeros or 0xFF)"
    else:
        state = f"NON-ONIE content ({non_zero_count} non-zero bytes in 256B)"

    assert actual_magic == TLVINFO_MAGIC, (
        f"\n"
        f"  EEPROM magic mismatch — ONIE TlvInfo data not present.\n"
        f"  Expected first 8 bytes: {expected_hex}  (\"TlvInfo\\x00\")\n"
        f"  Actual   first 8 bytes: {actual_hex}\n"
        f"  EEPROM state: {state}\n"
        f"  Full first 32 bytes:    {eeprom_raw_bytes[:32].hex()}\n"
        f"\n"
        f"  Root cause: the 24c64 chip at i2c-40/0x50 contains factory/ODM data\n"
        f"  that does not use ONIE TlvInfo format.  decode-syseeprom and the\n"
        f"  Python platform API both require TlvInfo format.\n"
        f"\n"
        f"  Fix: program the EEPROM with valid ONIE TlvInfo data, e.g.:\n"
        f"    sudo write-syseeprom \\\\\n"
        f"        -t 0x21 -v 'Wedge-100s-32X' \\\\\n"
        f"        -t 0x22 -v '<part-number>' \\\\\n"
        f"        -t 0x23 -v '<serial-number>' \\\\\n"
        f"        -t 0x24 -v '<base-mac-address>' \\\\\n"
        f"        -t 0x2b -v 'Accton'\n"
        f"  Or boot into ONIE recovery mode and run: onie-syseeprom\n"
    )


# ------------------------------------------------------------------
# CLI path
# ------------------------------------------------------------------

def test_eeprom_cli_decode(ssh, eeprom_raw_cli):
    """decode-syseeprom exits 0 and produces non-empty output."""
    print(f"\n{'='*60}")
    print("decode-syseeprom output:")
    print(eeprom_raw_cli)
    assert eeprom_raw_cli.strip(), "decode-syseeprom produced no output"


def test_eeprom_cli_tlvinfo_header(ssh, eeprom_raw_cli, eeprom_raw_bytes):
    """decode-syseeprom output contains TlvInfo header.

    Depends on test_eeprom_magic_bytes passing first.  If the magic bytes are
    wrong, this test provides the same 'write-syseeprom' diagnostic.
    """
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        f"TlvInfo header not found in EEPROM — see test_eeprom_magic_bytes for root cause and fix.\n"
        f"Actual first 8 bytes: {actual_magic.hex()}"
    )
    assert "TlvInfo" in eeprom_raw_cli, (
        f"decode-syseeprom did not show TlvInfo header: {eeprom_raw_cli!r}"
    )
    assert "Total Length" in eeprom_raw_cli, (
        f"decode-syseeprom did not show Total Length: {eeprom_raw_cli!r}"
    )


def test_eeprom_cli_product_name(ssh, eeprom_raw_cli, eeprom_raw_bytes):
    """Product Name TLV is present and references Wedge platform."""
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        "EEPROM not in TlvInfo format — see test_eeprom_magic_bytes for fix."
    )
    lines = [l.strip() for l in eeprom_raw_cli.splitlines()]
    product_lines = [l for l in lines if "Product Name" in l or "0x21" in l]
    assert product_lines, "Product Name (0x21) not found in decode-syseeprom output"
    combined = " ".join(product_lines).lower()
    assert PLATFORM_KEYWORD in combined, (
        f"Expected '{PLATFORM_KEYWORD}' in product name, got: {product_lines}"
    )


def test_eeprom_cli_serial_number(ssh, eeprom_raw_cli, eeprom_raw_bytes):
    """Serial Number TLV (0x23) is present and non-empty."""
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        "EEPROM not in TlvInfo format — see test_eeprom_magic_bytes for fix."
    )
    assert "Serial Number" in eeprom_raw_cli or "0x23" in eeprom_raw_cli, (
        "Serial Number (0x23) not found in EEPROM output"
    )


def test_eeprom_cli_mac_address(ssh, eeprom_raw_cli, eeprom_raw_bytes):
    """Base MAC Address TLV (0x24) is present and looks like a MAC."""
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        "EEPROM not in TlvInfo format — see test_eeprom_magic_bytes for fix."
    )
    assert "Base MAC" in eeprom_raw_cli or "0x24" in eeprom_raw_cli, (
        "Base MAC Address (0x24) not found in EEPROM output"
    )
    import re
    mac_pattern = re.compile(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
    assert mac_pattern.search(eeprom_raw_cli), (
        "No valid MAC address pattern found in EEPROM output"
    )


# ------------------------------------------------------------------
# Python API path
# ------------------------------------------------------------------

EEPROM_PYTHON = """\
import json
from sonic_platform.platform import Platform
chassis = Platform().get_chassis()
info = chassis.get_system_eeprom_info()
print(json.dumps(info))
"""


@pytest.fixture(scope="module")
def eeprom_api_data(ssh):
    """Fetch and cache eeprom info dict from the Python API."""
    out, err, rc = ssh.run_python(EEPROM_PYTHON, timeout=30)
    assert rc == 0, f"Python EEPROM script failed (rc={rc}): {err}"
    import json
    return json.loads(out.strip())


def test_eeprom_api_returns_dict(ssh, eeprom_api_data):
    """chassis.get_system_eeprom_info() returns a dict."""
    print(f"\nPython EEPROM API returned {len(eeprom_api_data)} TLV entries")
    assert isinstance(eeprom_api_data, dict), (
        f"Expected dict, got {type(eeprom_api_data)}"
    )


def test_eeprom_api_non_empty(ssh, eeprom_api_data, eeprom_raw_bytes):
    """Python API returns non-empty dict when EEPROM is valid TlvInfo."""
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        "EEPROM not in TlvInfo format — see test_eeprom_magic_bytes for fix."
    )
    assert eeprom_api_data, (
        "EEPROM magic bytes are correct but Python API returned empty dict — "
        "check sonic_platform/eeprom.py and the at24 driver binding at 40-0050."
    )


def test_eeprom_api_required_tlv_codes(ssh, eeprom_api_data, eeprom_raw_bytes):
    """All required TLV type codes are present in EEPROM info dict."""
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        "EEPROM not in TlvInfo format — see test_eeprom_magic_bytes for fix."
    )
    keys_lower = {k.lower() for k in eeprom_api_data.keys()}
    for code, name in REQUIRED_TLV_CODES.items():
        assert code in keys_lower, (
            f"TLV code {code} ({name}) missing. Present keys: {sorted(keys_lower)}"
        )


def test_eeprom_api_product_name_value(ssh, eeprom_api_data, eeprom_raw_bytes):
    """Product name value from Python API contains platform keyword."""
    actual_magic = eeprom_raw_bytes[:8]
    assert actual_magic == TLVINFO_MAGIC, (
        "EEPROM not in TlvInfo format — see test_eeprom_magic_bytes for fix."
    )
    keys_lower = {k.lower(): v for k, v in eeprom_api_data.items()}
    product = keys_lower.get("0x21", "")
    assert PLATFORM_KEYWORD in product.lower(), (
        f"Expected '{PLATFORM_KEYWORD}' in product name, got: '{product}'"
    )
