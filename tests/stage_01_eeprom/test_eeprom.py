"""Stage 01 — System EEPROM (TLV dump).

Tests the system EEPROM (24c64 at i2c-40/0x50) on the Accton Wedge 100S-32X.

Architecture note:
  The EEPROM is a 24c64 chip at 0x50, registered on i2c-40 (PCA9548 mux 0x74
  channel 6) via the CP2112 USB-HID bridge (i2c-1). After xcvrd starts, direct
  at24 sysfs reads are unreliable due to I2C bus contention on the shared CP2112
  bridge — the kernel at24 driver returns zeros rather than blocking.

  Platform-init (wedge100s-platform-init.service) writes a raw copy of the
  EEPROM to /var/run/platform_cache/syseeprom_cache before pmon/xcvrd start.
  decode-syseeprom and sonic_platform both read from this cache at runtime.

Phase reference: Phase 7 (System EEPROM).
"""

import re
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
# Phase 2: daemon writes syseeprom via hidraw; sysfs device does not exist.
EEPROM_DAEMON_CACHE = "/run/wedge100s/syseeprom"


# ------------------------------------------------------------------
# Module-scoped helpers
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def eeprom_cache_bytes(ssh):
    """Read the first 256 bytes of the daemon EEPROM cache file."""
    script = f"""
data = open('{EEPROM_DAEMON_CACHE}', 'rb').read()
print(data[:256].hex())
"""
    out, err, rc = ssh.run_python(script, timeout=20)
    assert rc == 0, f"Could not read EEPROM daemon cache: {err}"
    return bytes.fromhex(out.strip())


@pytest.fixture(scope="module")
def eeprom_raw_cli(ssh):
    """Run decode-syseeprom once and return its output."""
    out, _, _ = ssh.run("sudo decode-syseeprom 2>&1")
    return out


# ------------------------------------------------------------------
# Sysfs device registration
# ------------------------------------------------------------------

def test_eeprom_daemon_cache_exists(ssh):
    """Daemon cache /run/wedge100s/syseeprom exists (Phase 2: written via hidraw).

    Written by wedge100s-i2c-daemon (OnBootSec=5s) via CP2112 hidraw direct read.
    i2c_mux_pca954x and at24 are not loaded; the sysfs device 40-0050 does not exist.
    If missing: sudo systemctl start wedge100s-i2c-poller.service
    """
    out, err, rc = ssh.run(f"ls -la {EEPROM_DAEMON_CACHE} 2>/dev/null")
    assert rc == 0, (
        f"Daemon EEPROM cache missing: {EEPROM_DAEMON_CACHE}\n"
        "Fix: sudo systemctl start wedge100s-i2c-poller.service"
    )


def test_eeprom_daemon_cache_size(ssh):
    """Daemon cache is 8192 bytes (full 24c64 image)."""
    out, err, rc = ssh.run(f"wc -c < {EEPROM_DAEMON_CACHE} 2>/dev/null")
    assert rc == 0, f"Could not read daemon cache size: {err}"
    assert int(out.strip()) == 8192, (
        f"Expected 8192 bytes, got {out.strip()}"
    )


# ------------------------------------------------------------------
# Cache file — written at boot before xcvrd starts
# ------------------------------------------------------------------

def test_eeprom_cache_magic_bytes(ssh, eeprom_cache_bytes):
    """Cache file starts with ONIE TlvInfo magic bytes.

    If the cache was written while the I2C bus was hung (all-zero reads),
    the cache itself will be invalid. Fix: reboot to reset the CP2112 bridge,
    then restart wedge100s-platform-init before xcvrd starts.
    """
    actual = eeprom_cache_bytes[:8]
    assert actual == TLVINFO_MAGIC, (
        f"Cache has wrong magic bytes — written during bus contention?\n"
        f"  Expected: {TLVINFO_MAGIC.hex()}  ('TlvInfo\\x00')\n"
        f"  Actual:   {actual.hex()}\n"
        f"  Fix: reboot, then 'sudo systemctl restart wedge100s-platform-init'"
    )


# ------------------------------------------------------------------
# CLI path
# ------------------------------------------------------------------

def test_eeprom_cli_runs(ssh, eeprom_raw_cli):
    """decode-syseeprom produces non-empty output."""
    assert eeprom_raw_cli.strip(), "decode-syseeprom produced no output"


def test_eeprom_cli_tlvinfo_header(ssh, eeprom_raw_cli):
    """decode-syseeprom output contains TlvInfo header and Total Length."""
    assert "TlvInfo" in eeprom_raw_cli, (
        f"TlvInfo header not in output: {eeprom_raw_cli!r}"
    )
    assert "Total Length" in eeprom_raw_cli, (
        f"Total Length not in output: {eeprom_raw_cli!r}"
    )


def test_eeprom_cli_product_name(ssh, eeprom_raw_cli):
    """Product Name TLV (0x21) is present and contains 'wedge'."""
    lines = [l.strip() for l in eeprom_raw_cli.splitlines()]
    product_lines = [l for l in lines if "Product Name" in l or "0x21" in l]
    assert product_lines, "Product Name (0x21) not found in decode-syseeprom output"
    assert PLATFORM_KEYWORD in " ".join(product_lines).lower(), (
        f"Expected '{PLATFORM_KEYWORD}' in product name, got: {product_lines}"
    )


def test_eeprom_cli_serial_number(ssh, eeprom_raw_cli):
    """Serial Number TLV (0x23) is present."""
    assert "Serial Number" in eeprom_raw_cli or "0x23" in eeprom_raw_cli, (
        "Serial Number (0x23) not found in decode-syseeprom output"
    )


def test_eeprom_cli_mac_address(ssh, eeprom_raw_cli):
    """Base MAC Address TLV (0x24) is present and is a valid MAC."""
    assert "Base MAC" in eeprom_raw_cli or "0x24" in eeprom_raw_cli, (
        "Base MAC Address (0x24) not found in decode-syseeprom output"
    )
    mac_pattern = re.compile(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
    assert mac_pattern.search(eeprom_raw_cli), (
        "No valid MAC address pattern found in decode-syseeprom output"
    )


def test_eeprom_cli_crc(ssh, eeprom_raw_cli):
    """CRC-32 TLV (0xFE) is present in decode-syseeprom output."""
    assert "CRC-32" in eeprom_raw_cli or "0xFE" in eeprom_raw_cli, (
        "CRC-32 (0xFE) not found in decode-syseeprom output"
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
    """Fetch eeprom info dict from sonic_platform Python API."""
    out, err, rc = ssh.run_python(EEPROM_PYTHON, timeout=30)
    assert rc == 0, f"Python EEPROM script failed (rc={rc}): {err}"
    import json
    return json.loads(out.strip())


def test_eeprom_api_returns_dict(ssh, eeprom_api_data):
    """chassis.get_system_eeprom_info() returns a non-empty dict."""
    assert isinstance(eeprom_api_data, dict) and eeprom_api_data, (
        f"Expected non-empty dict, got: {eeprom_api_data!r}"
    )


def test_eeprom_api_required_tlv_codes(ssh, eeprom_api_data):
    """All required TLV type codes are present in EEPROM info dict."""
    keys_lower = {k.lower() for k in eeprom_api_data.keys()}
    for code, name in REQUIRED_TLV_CODES.items():
        assert code in keys_lower, (
            f"TLV code {code} ({name}) missing. Present keys: {sorted(keys_lower)}"
        )


def test_eeprom_api_product_name_value(ssh, eeprom_api_data):
    """Product Name (0x21) from Python API contains 'wedge'."""
    keys_lower = {k.lower(): v for k, v in eeprom_api_data.items()}
    product = keys_lower.get("0x21", "")
    assert PLATFORM_KEYWORD in product.lower(), (
        f"Expected '{PLATFORM_KEYWORD}' in product name, got: '{product}'"
    )


def test_eeprom_api_crc_present(ssh, eeprom_api_data):
    """CRC-32 TLV (0xFE) is present in Python API output."""
    keys_lower = {k.lower() for k in eeprom_api_data.keys()}
    assert "0xfe" in keys_lower, (
        f"CRC-32 (0xFE) missing. Present keys: {sorted(keys_lower)}"
    )
