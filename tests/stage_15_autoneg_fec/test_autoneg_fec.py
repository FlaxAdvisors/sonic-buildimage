"""Stage 15 — Auto-Negotiation & FEC Configuration.

Verifies FEC and auto-negotiation configuration on the Wedge 100S-32X
Tomahawk platform.  Tests config CLI acceptance, CONFIG_DB/APP_DB
propagation, and ASIC_DB programming.

Hardware findings (verified 2026-03-02):
  - FEC modes supported by this SAI: 'rs' (RS-FEC CL91), 'none'
  - FEC mode 'fc' (FC-FEC CL74) is REJECTED — not in this SAI's allowed set
  - Auto-negotiation: CLI and CONFIG_DB accept it, but ASIC_DB
    SAI_PORT_ATTR_AUTO_NEG_MODE stays false — Broadcom SAI on this
    Tomahawk does not implement AN at the hardware level
  - BCM config: phy_an_c73=0x0 (Clause 73 disabled), phy_an_c37=0x3
  - Supported speeds (STATE_DB): 40000, 100000

Topology (from stage_13):
  4 ports connected to rabbit-lorax (Arista EOS) via 100G DAC:
    Ethernet16, Ethernet32, Ethernet48, Ethernet112
  All require RS-FEC (CL91) for link.

Phase reference: Phase 15 (Auto-Negotiation & FEC Configuration).
"""

import time
import pytest


# Port used for config-change tests — disconnected, safe to modify
TEST_PORT = "Ethernet0"

# Connected ports with RS-FEC already configured
CONNECTED_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]

# Speeds supported by this Tomahawk platform (from STATE_DB supported_speeds)
SUPPORTED_SPEEDS = ["40000", "100000"]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _config_db_get(ssh, port, field):
    """Read a field from CONFIG_DB PORT table."""
    out, _, rc = ssh.run(
        f"redis-cli -n 4 hget 'PORT|{port}' {field}", timeout=10
    )
    return out.strip() if rc == 0 else None


def _app_db_get(ssh, port, field):
    """Read a field from APP_DB PORT_TABLE."""
    out, _, rc = ssh.run(
        f"redis-cli -n 0 hget 'PORT_TABLE:{port}' {field}", timeout=10
    )
    return out.strip() if rc == 0 else None


def _asic_db_port_attr(ssh, port, attr):
    """Read a SAI port attribute from ASIC_DB for a given port name."""
    oid_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10
    )
    oid = oid_out.strip()
    if not oid:
        return None
    out, _, _ = ssh.run(
        f"redis-cli -n 1 hget 'ASIC_STATE:SAI_OBJECT_TYPE_PORT:{oid}' {attr}",
        timeout=10,
    )
    return out.strip() if out else None


def _cleanup_port(ssh, port):
    """Remove AN/FEC test artifacts from CONFIG_DB for a port."""
    ssh.run(
        f"redis-cli -n 4 hdel 'PORT|{port}' autoneg fec adv_speeds adv_interface_types",
        timeout=10,
    )
    time.sleep(1)


# ------------------------------------------------------------------
# FEC configuration — existing state on connected ports
# ------------------------------------------------------------------

class TestFecConnectedPorts:
    """Verify RS-FEC is properly configured on connected (linked-up) ports."""

    def test_connected_ports_fec_rs_in_config_db(self, ssh):
        """Connected ports have fec=rs in CONFIG_DB (set during Phase 13)."""
        for port in CONNECTED_PORTS:
            fec = _config_db_get(ssh, port, "fec")
            print(f"  {port}: CONFIG_DB fec={fec!r}")
            assert fec == "rs", (
                f"{port}: CONFIG_DB fec={fec!r}, expected 'rs'.\n"
                f"Fix: sudo config interface fec {port} rs && sudo config save -y"
            )

    def test_connected_ports_fec_rs_in_asic_db(self, ssh):
        """Connected ports show SAI_PORT_FEC_MODE_RS in ASIC_DB."""
        for port in CONNECTED_PORTS:
            val = _asic_db_port_attr(ssh, port, "SAI_PORT_ATTR_FEC_MODE")
            print(f"  {port}: ASIC_DB FEC_MODE={val!r}")
            assert val == "SAI_PORT_FEC_MODE_RS", (
                f"{port}: ASIC_DB FEC_MODE={val!r}, expected SAI_PORT_FEC_MODE_RS.\n"
                "RS-FEC must be applied at ASIC level for 100G-CR4 link to Arista."
            )


# ------------------------------------------------------------------
# FEC configuration — config change tests on disconnected port
# ------------------------------------------------------------------

class TestFecConfig:
    """Test FEC configuration changes on disconnected port (Ethernet0)."""

    def test_fec_rs_accepted(self, ssh):
        """config interface fec Ethernet0 rs succeeds and propagates."""
        try:
            out, err, rc = ssh.run(
                f"sudo config interface fec {TEST_PORT} rs", timeout=15
            )
            assert rc == 0, f"FEC rs config failed (rc={rc}): {err}"

            time.sleep(1)
            config_fec = _config_db_get(ssh, TEST_PORT, "fec")
            print(f"  CONFIG_DB fec={config_fec!r}")
            assert config_fec == "rs", (
                f"CONFIG_DB fec={config_fec!r} after setting rs"
            )

            asic_fec = _asic_db_port_attr(
                ssh, TEST_PORT, "SAI_PORT_ATTR_FEC_MODE"
            )
            print(f"  ASIC_DB FEC_MODE={asic_fec!r}")
            assert asic_fec == "SAI_PORT_FEC_MODE_RS", (
                f"ASIC_DB FEC_MODE={asic_fec!r}, expected SAI_PORT_FEC_MODE_RS"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_fec_none_accepted(self, ssh):
        """config interface fec Ethernet0 none clears FEC."""
        try:
            # First set rs, then clear to none
            ssh.run(f"sudo config interface fec {TEST_PORT} rs", timeout=15)
            time.sleep(1)

            out, err, rc = ssh.run(
                f"sudo config interface fec {TEST_PORT} none", timeout=15
            )
            assert rc == 0, f"FEC none config failed (rc={rc}): {err}"

            time.sleep(1)
            config_fec = _config_db_get(ssh, TEST_PORT, "fec")
            print(f"  CONFIG_DB fec={config_fec!r}")
            assert config_fec == "none", (
                f"CONFIG_DB fec={config_fec!r} after setting none"
            )

            asic_fec = _asic_db_port_attr(
                ssh, TEST_PORT, "SAI_PORT_ATTR_FEC_MODE"
            )
            print(f"  ASIC_DB FEC_MODE={asic_fec!r}")
            assert asic_fec == "SAI_PORT_FEC_MODE_NONE", (
                f"ASIC_DB FEC_MODE={asic_fec!r}, expected SAI_PORT_FEC_MODE_NONE"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_fec_fc_rejected(self, ssh):
        """config interface fec Ethernet0 fc is rejected (not supported on TH).

        The Tomahawk SAI on this platform only supports 'rs' and 'none'.
        FC-FEC (CL74 / FireCode) is for 25G/10G SerDes — not applicable
        to the 100G-only configuration here.
        """
        out, err, rc = ssh.run(
            f"sudo config interface fec {TEST_PORT} fc", timeout=15
        )
        combined = (out + err).lower()
        print(f"  rc={rc}, output: {(out + err).strip()!r}")
        assert rc != 0 or "not in" in combined or "invalid" in combined, (
            f"Expected fc to be rejected, but got rc={rc}: {out + err}"
        )


# ------------------------------------------------------------------
# Auto-negotiation configuration
# ------------------------------------------------------------------

class TestAutonegConfig:
    """Test auto-negotiation configuration on disconnected port (Ethernet0).

    Key finding: The Broadcom SAI on this Tomahawk accepts autoneg
    configuration at the CONFIG_DB/APP_DB level but does NOT program
    SAI_PORT_ATTR_AUTO_NEG_MODE=true into ASIC_DB.  The BCM config
    has phy_an_c73=0x0 (Clause 73 AN disabled at firmware level).
    """

    def test_autoneg_enable_accepted(self, ssh):
        """config interface autoneg Ethernet0 enabled succeeds."""
        try:
            out, err, rc = ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            assert rc == 0, f"autoneg enable failed (rc={rc}): {err}"
            print(f"  CLI accepted autoneg enabled (rc={rc})")
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_autoneg_enable_propagates_to_config_db(self, ssh):
        """Enabling autoneg sets autoneg=on in CONFIG_DB."""
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(1)

            val = _config_db_get(ssh, TEST_PORT, "autoneg")
            print(f"  CONFIG_DB autoneg={val!r}")
            assert val == "on", (
                f"CONFIG_DB autoneg={val!r}, expected 'on'"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_autoneg_enable_propagates_to_app_db(self, ssh):
        """Enabling autoneg sets autoneg=on in APP_DB PORT_TABLE."""
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(2)

            val = _app_db_get(ssh, TEST_PORT, "autoneg")
            print(f"  APP_DB autoneg={val!r}")
            assert val == "on", (
                f"APP_DB autoneg={val!r}, expected 'on'"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_autoneg_not_applied_in_asic_db(self, ssh):
        """ASIC_DB SAI_PORT_ATTR_AUTO_NEG_MODE stays false (SAI limitation).

        The Broadcom SAI on this Tomahawk does not actually program
        auto-negotiation into hardware.  The BCM config has
        phy_an_c73=0x0 (Clause 73 AN disabled).  This is a known
        limitation, not a bug.
        """
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(2)

            val = _asic_db_port_attr(
                ssh, TEST_PORT, "SAI_PORT_ATTR_AUTO_NEG_MODE"
            )
            print(f"  ASIC_DB AUTO_NEG_MODE={val!r}")
            # On this platform, SAI silently keeps AN disabled
            assert val == "false", (
                f"ASIC_DB AUTO_NEG_MODE={val!r} — unexpected! If this changed "
                "to 'true', the SAI may now support AN. Update this test and "
                "test link behavior with Arista peer."
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_autoneg_disable_accepted(self, ssh):
        """config interface autoneg Ethernet0 disabled succeeds."""
        try:
            # Enable first, then disable
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(1)

            out, err, rc = ssh.run(
                f"sudo config interface autoneg {TEST_PORT} disabled", timeout=15
            )
            assert rc == 0, f"autoneg disable failed (rc={rc}): {err}"

            time.sleep(1)
            val = _config_db_get(ssh, TEST_PORT, "autoneg")
            print(f"  CONFIG_DB autoneg={val!r}")
            assert val == "off", (
                f"CONFIG_DB autoneg={val!r} after disabling, expected 'off'"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_show_autoneg_status(self, ssh):
        """show interfaces autoneg status shows enabled after config."""
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(2)

            out, err, rc = ssh.run(
                f"show interfaces autoneg status {TEST_PORT}", timeout=15
            )
            assert rc == 0, f"show autoneg status failed (rc={rc}): {err}"
            print(f"\n{out}")
            assert "enabled" in out.lower(), (
                f"Expected 'enabled' in autoneg status output:\n{out}"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)


# ------------------------------------------------------------------
# Advertised speeds
# ------------------------------------------------------------------

class TestAdvertisedSpeeds:
    """Test advertised-speeds and advertised-types configuration."""

    def test_supported_speeds_in_state_db(self, ssh):
        """STATE_DB has supported_speeds for ports (40G, 100G on Tomahawk)."""
        out, _, rc = ssh.run(
            f"redis-cli -n 6 hget 'PORT_TABLE|{TEST_PORT}' supported_speeds",
            timeout=10,
        )
        val = out.strip()
        print(f"  {TEST_PORT} supported_speeds={val!r}")
        if not val:
            pytest.skip("supported_speeds not populated in STATE_DB")
        speeds = val.split(",")
        assert "100000" in speeds, (
            f"100000 not in supported_speeds: {val}"
        )

    def test_advertised_speeds_accepted(self, ssh):
        """config interface advertised-speeds sets adv_speeds in CONFIG_DB."""
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(1)

            out, err, rc = ssh.run(
                f"sudo config interface advertised-speeds {TEST_PORT} 40000,100000",
                timeout=15,
            )
            assert rc == 0, (
                f"advertised-speeds config failed (rc={rc}): {err}"
            )

            time.sleep(1)
            val = _config_db_get(ssh, TEST_PORT, "adv_speeds")
            print(f"  CONFIG_DB adv_speeds={val!r}")
            assert val == "40000,100000", (
                f"CONFIG_DB adv_speeds={val!r}, expected '40000,100000'"
            )

            app_val = _app_db_get(ssh, TEST_PORT, "adv_speeds")
            print(f"  APP_DB adv_speeds={app_val!r}")
            assert app_val == "40000,100000", (
                f"APP_DB adv_speeds={app_val!r}, expected '40000,100000'"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_advertised_speeds_shown_in_cli(self, ssh):
        """show interfaces autoneg status shows advertised speeds."""
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(1)
            ssh.run(
                f"sudo config interface advertised-speeds {TEST_PORT} 40000,100000",
                timeout=15,
            )
            time.sleep(2)

            out, err, rc = ssh.run(
                f"show interfaces autoneg status {TEST_PORT}", timeout=15
            )
            assert rc == 0, f"show autoneg status failed (rc={rc}): {err}"
            print(f"\n{out}")
            # The CLI displays speeds in human-readable format (40G, 100G)
            assert "40G" in out or "40000" in out, (
                f"Expected advertised speed 40G in output:\n{out}"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)

    def test_advertised_types_accepted(self, ssh):
        """config interface advertised-types sets adv_interface_types in CONFIG_DB."""
        try:
            ssh.run(
                f"sudo config interface autoneg {TEST_PORT} enabled", timeout=15
            )
            time.sleep(1)

            out, err, rc = ssh.run(
                f"sudo config interface advertised-types {TEST_PORT} CR4",
                timeout=15,
            )
            assert rc == 0, (
                f"advertised-types config failed (rc={rc}): {err}"
            )

            time.sleep(1)
            val = _config_db_get(ssh, TEST_PORT, "adv_interface_types")
            print(f"  CONFIG_DB adv_interface_types={val!r}")
            assert val == "CR4", (
                f"CONFIG_DB adv_interface_types={val!r}, expected 'CR4'"
            )
        finally:
            _cleanup_port(ssh, TEST_PORT)


# ------------------------------------------------------------------
# Default state — ports without AN/FEC config
# ------------------------------------------------------------------

class TestDefaultState:
    """Verify default AN/FEC state for unconfigured ports."""

    def test_default_autoneg_is_not_set(self, ssh):
        """Ports without explicit autoneg config show N/A in CLI."""
        # Use a port that has never had autoneg configured (Ethernet4)
        out, err, rc = ssh.run(
            "show interfaces autoneg status Ethernet4", timeout=15
        )
        assert rc == 0, f"show autoneg status failed (rc={rc}): {err}"
        print(f"\n{out}")
        assert "N/A" in out, (
            f"Expected 'N/A' for unconfigured autoneg on Ethernet4:\n{out}"
        )

    def test_default_asic_autoneg_false(self, ssh):
        """ASIC_DB shows AUTO_NEG_MODE=false by default."""
        val = _asic_db_port_attr(
            ssh, "Ethernet4", "SAI_PORT_ATTR_AUTO_NEG_MODE"
        )
        print(f"  Ethernet4 ASIC_DB AUTO_NEG_MODE={val!r}")
        assert val == "false", (
            f"Unexpected ASIC_DB AUTO_NEG_MODE={val!r} for unconfigured port"
        )

    def test_connected_ports_autoneg_status(self, ssh):
        """Connected ports (with FEC but no AN) show correct autoneg status."""
        for port in CONNECTED_PORTS:
            an_val = _config_db_get(ssh, port, "autoneg")
            print(f"  {port}: CONFIG_DB autoneg={an_val!r}")
            # AN should be unset or 'off' on connected ports
            assert an_val in (None, "", "off"), (
                f"{port}: autoneg={an_val!r} — unexpected. These ports use "
                "static FEC (rs) without auto-negotiation."
            )
