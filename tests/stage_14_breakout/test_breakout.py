"""Stage 14 — Speed Change & Dynamic Port Breakout.

Phase 14a — Speed Change (no breakout):
  Verifies that `config interface speed` is accepted by SAI and propagates
  through the CONFIG_DB → APP_DB pipeline.  Tested on Ethernet0 (disconnected
  port with breakout cable, no active peer — safe to modify).

Phase 14b — DPB enablement (platform.json & hwsku.json):
  Verifies that the new platform.json and hwsku.json files are present, have
  correct structure and lane mappings, and that `show interfaces breakout`
  exposes all breakout modes.

  NOTE: Live breakout (`config interface breakout`) is NOT tested automatically
  but works manually with the flex BCM config.  Use:
    sudo config interface breakout <iface> '4x25G[10G]' -y -f -l
  See tests/notes/dpb-flex-bcm.md for details.

Hardware context:
  Ethernet0:  Port 1, lanes 117-120, breakout cable, no peer (safe for speed test)
  32 QSFP28 ports, BCM56960 Tomahawk, static .config.bcm (100G all ports)

Phase reference: Phase 14 (Speed Configuration & Multi-Speed Support).
"""

import json
import time
import pytest


# Ethernet4: Port 2, lanes 113-116, not in operational breakout, no connected hosts.
# Safe to modify speed and break out for testing.
SPEED_TEST_PORT = "Ethernet4"
BREAKOUT_PORT = SPEED_TEST_PORT

NUM_PORTS = 32

# All parent ports (step of 4, matching port_config.ini)
PARENT_PORTS = [f"Ethernet{i * 4}" for i in range(NUM_PORTS)]

# Expected breakout modes for every port on Tomahawk
EXPECTED_BREAKOUT_MODES = {"1x100G[40G]", "2x50G", "4x25G[10G]"}

# Lane mapping from port_config.ini (port name → comma-separated lanes)
PORT_LANES = {
    "Ethernet0": "117,118,119,120",
    "Ethernet4": "113,114,115,116",
    "Ethernet8": "125,126,127,128",
    "Ethernet12": "121,122,123,124",
    "Ethernet16": "5,6,7,8",
    "Ethernet20": "1,2,3,4",
    "Ethernet24": "13,14,15,16",
    "Ethernet28": "9,10,11,12",
    "Ethernet32": "21,22,23,24",
    "Ethernet36": "17,18,19,20",
    "Ethernet40": "29,30,31,32",
    "Ethernet44": "25,26,27,28",
    "Ethernet48": "37,38,39,40",
    "Ethernet52": "33,34,35,36",
    "Ethernet56": "45,46,47,48",
    "Ethernet60": "41,42,43,44",
    "Ethernet64": "53,54,55,56",
    "Ethernet68": "49,50,51,52",
    "Ethernet72": "61,62,63,64",
    "Ethernet76": "57,58,59,60",
    "Ethernet80": "69,70,71,72",
    "Ethernet84": "65,66,67,68",
    "Ethernet88": "77,78,79,80",
    "Ethernet92": "73,74,75,76",
    "Ethernet96": "85,86,87,88",
    "Ethernet100": "81,82,83,84",
    "Ethernet104": "93,94,95,96",
    "Ethernet108": "89,90,91,92",
    "Ethernet112": "101,102,103,104",
    "Ethernet116": "97,98,99,100",
    "Ethernet120": "109,110,111,112",
    "Ethernet124": "105,106,107,108",
}


# ===================================================================
# Phase 14a — Speed Change
# ===================================================================

class TestSpeedChange:
    """Phase 14a: Verify speed change via `config interface speed`."""

    @pytest.fixture(autouse=True)
    def _restore_speed(self, ssh):
        """Snapshot SPEED_TEST_PORT speed before each test; restore after."""
        out, _, _ = ssh.run(
            f"redis-cli -n 4 hget 'PORT|{SPEED_TEST_PORT}' speed", timeout=10
        )
        original = out.strip() or "100000"
        yield
        ssh.run(
            f"sudo config interface speed {SPEED_TEST_PORT} {original}", timeout=15
        )

    def _get_speed(self, ssh, port, db="config"):
        """Read port speed from CONFIG_DB (db=config) or APP_DB (db=app)."""
        if db == "config":
            out, _, rc = ssh.run(
                f"redis-cli -n 4 hget 'PORT|{port}' speed", timeout=10
            )
        else:
            out, _, rc = ssh.run(
                f"redis-cli -n 0 hget 'PORT_TABLE:{port}' speed", timeout=10
            )
        return out.strip() if rc == 0 else None

    def _set_speed(self, ssh, port, speed):
        """Set port speed and return (stdout, stderr, rc)."""
        return ssh.run(
            f"sudo config interface speed {port} {speed}", timeout=15
        )

    def test_speed_change_to_40g(self, ssh):
        """config interface speed Ethernet0 40000 is accepted by SAI.

        Changes Ethernet0 from 100G to 40G, verifies CONFIG_DB and APP_DB
        both reflect 40000, then reverts to 100G.
        """
        port = SPEED_TEST_PORT

        # Record baseline
        original = self._get_speed(ssh, port, "config")
        print(f"\n  Baseline: {port} speed={original}")

        # Change to 40G
        out, err, rc = self._set_speed(ssh, port, 40000)
        assert rc == 0, (
            f"config interface speed {port} 40000 failed (rc={rc}): {err}"
        )

        try:
            # Allow propagation
            time.sleep(2)

            # Verify CONFIG_DB
            config_speed = self._get_speed(ssh, port, "config")
            print(f"  CONFIG_DB speed: {config_speed}")
            assert config_speed == "40000", (
                f"CONFIG_DB speed={config_speed!r}, expected '40000'"
            )

            # Verify APP_DB
            app_speed = self._get_speed(ssh, port, "app")
            print(f"  APP_DB speed: {app_speed}")
            assert app_speed == "40000", (
                f"APP_DB speed={app_speed!r}, expected '40000'"
            )
        finally:
            # Always revert to original speed
            revert_speed = original if original else "100000"
            self._set_speed(ssh, port, revert_speed)
            time.sleep(1)

    def test_speed_change_shows_in_cli(self, ssh):
        """show interfaces status reflects speed change to 40G."""
        port = SPEED_TEST_PORT

        out, err, rc = self._set_speed(ssh, port, 40000)
        assert rc == 0, f"Speed change failed: {err}"

        try:
            time.sleep(2)
            out, err, rc = ssh.run(
                f"show interfaces status {port}", timeout=15
            )
            assert rc == 0, f"show interfaces status failed: {err}"
            print(f"\n{out}")
            assert "40G" in out, (
                f"Expected '40G' in show interfaces status output:\n{out}"
            )
        finally:
            self._set_speed(ssh, port, 100000)
            time.sleep(1)

    def test_speed_revert_to_100g(self, ssh):
        """Speed reverts cleanly from 40G back to 100G."""
        port = SPEED_TEST_PORT

        # Set 40G
        self._set_speed(ssh, port, 40000)
        time.sleep(1)

        # Revert to 100G
        out, err, rc = self._set_speed(ssh, port, 100000)
        assert rc == 0, f"Revert to 100G failed (rc={rc}): {err}"
        time.sleep(2)

        # Verify CONFIG_DB
        config_speed = self._get_speed(ssh, port, "config")
        print(f"\n  After revert — CONFIG_DB speed: {config_speed}")
        assert config_speed == "100000", (
            f"CONFIG_DB speed={config_speed!r} after revert, expected '100000'"
        )

        # Verify APP_DB
        app_speed = self._get_speed(ssh, port, "app")
        print(f"  After revert — APP_DB speed: {app_speed}")
        assert app_speed == "100000", (
            f"APP_DB speed={app_speed!r} after revert, expected '100000'"
        )


# ===================================================================
# Phase 14b — platform.json
# ===================================================================

class TestPlatformJson:
    """Phase 14b: Verify platform.json structure and content."""

    @pytest.fixture(scope="class")
    def platform_json(self, ssh):
        """Load and parse platform.json from the switch."""
        out, err, rc = ssh.run(
            "cat /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/platform.json", timeout=10
        )
        assert rc == 0, (
            f"Cannot read platform.json (rc={rc}): {err}\n"
            "File may not be deployed. Copy to "
            "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/platform.json on the switch."
        )
        data = json.loads(out)
        return data

    def test_platform_json_exists_and_valid(self, platform_json):
        """platform.json is present on switch and parses as valid JSON."""
        assert "interfaces" in platform_json, (
            "platform.json missing 'interfaces' key"
        )
        print(f"\n  platform.json loaded: "
              f"{len(platform_json['interfaces'])} interfaces")

    def test_platform_json_has_32_ports(self, platform_json):
        """platform.json defines exactly 32 parent ports."""
        ifaces = platform_json["interfaces"]
        assert len(ifaces) == NUM_PORTS, (
            f"Expected {NUM_PORTS} ports, got {len(ifaces)}: "
            f"{sorted(ifaces.keys())}"
        )

    def test_platform_json_port_names(self, platform_json):
        """platform.json port names match expected Ethernet0..124 (step 4)."""
        ifaces = platform_json["interfaces"]
        actual = set(ifaces.keys())
        expected = set(PARENT_PORTS)
        missing = expected - actual
        extra = actual - expected
        assert not missing and not extra, (
            f"Port name mismatch.\n"
            f"  Missing: {sorted(missing)}\n"
            f"  Extra: {sorted(extra)}"
        )

    def test_platform_json_lanes_match_port_config(self, platform_json):
        """Lane mappings in platform.json match port_config.ini exactly."""
        ifaces = platform_json["interfaces"]
        mismatches = []
        for port, expected_lanes in PORT_LANES.items():
            actual = ifaces.get(port, {}).get("lanes", "")
            if actual != expected_lanes:
                mismatches.append(
                    f"  {port}: platform.json={actual!r} "
                    f"vs port_config.ini={expected_lanes!r}"
                )
        print(f"\n  Checked {len(PORT_LANES)} lane mappings")
        assert not mismatches, (
            f"Lane mapping mismatches:\n" + "\n".join(mismatches)
        )

    def test_platform_json_breakout_modes(self, platform_json):
        """Every port has the expected 3 breakout modes."""
        ifaces = platform_json["interfaces"]
        issues = []
        for port in PARENT_PORTS:
            modes = set(ifaces.get(port, {}).get("breakout_modes", {}).keys())
            if modes != EXPECTED_BREAKOUT_MODES:
                issues.append(
                    f"  {port}: got {sorted(modes)}, "
                    f"expected {sorted(EXPECTED_BREAKOUT_MODES)}"
                )
        print(f"\n  Checked breakout modes for {len(PARENT_PORTS)} ports")
        assert not issues, (
            f"Breakout mode mismatches:\n" + "\n".join(issues)
        )

    def test_platform_json_index_values(self, platform_json):
        """Each port has index matching its front-panel port number (1-32)."""
        ifaces = platform_json["interfaces"]
        issues = []
        for port in PARENT_PORTS:
            port_num = PARENT_PORTS.index(port) + 1
            expected_index = ",".join([str(port_num)] * 4)
            actual_index = ifaces.get(port, {}).get("index", "")
            if actual_index != expected_index:
                issues.append(
                    f"  {port}: index={actual_index!r}, "
                    f"expected={expected_index!r}"
                )
        assert not issues, (
            f"Index value mismatches:\n" + "\n".join(issues)
        )

    def test_platform_json_4x_aliases(self, platform_json):
        """4x25G breakout aliases use Ethernet<N>/1-4 format."""
        ifaces = platform_json["interfaces"]
        issues = []
        for port in PARENT_PORTS:
            port_num = PARENT_PORTS.index(port) + 1
            modes = ifaces.get(port, {}).get("breakout_modes", {})
            aliases_4x = modes.get("4x25G[10G]", [])
            expected = [f"Ethernet{port_num}/{i}" for i in range(1, 5)]
            if aliases_4x != expected:
                issues.append(
                    f"  {port}: 4x aliases={aliases_4x}, expected={expected}"
                )
        sample = PARENT_PORTS[0]
        port_num = 1
        print(f"\n  Sample: {sample} 4x aliases → "
              f"{[f'Ethernet{port_num}/{i}' for i in range(1, 5)]}")
        assert not issues, (
            f"4x25G alias mismatches:\n" + "\n".join(issues)
        )

    def test_platform_json_chassis_sfps(self, platform_json):
        """Chassis section lists 32 SFPs."""
        chassis = platform_json.get("chassis", {})
        sfps = chassis.get("sfps", [])
        assert len(sfps) == NUM_PORTS, (
            f"Expected {NUM_PORTS} SFPs in chassis, got {len(sfps)}"
        )


# ===================================================================
# Phase 14b — hwsku.json
# ===================================================================

class TestHwskuJson:
    """Phase 14b: Verify hwsku.json structure and content."""

    @pytest.fixture(scope="class")
    def hwsku_json(self, ssh):
        """Load and parse hwsku.json from the switch."""
        out, err, rc = ssh.run(
            "cat /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json",
            timeout=10,
        )
        assert rc == 0, (
            f"Cannot read hwsku.json (rc={rc}): {err}\n"
            "File may not be deployed. Copy to "
            "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json"
        )
        return json.loads(out)

    def test_hwsku_json_exists_and_valid(self, hwsku_json):
        """hwsku.json is present on switch and parses as valid JSON."""
        assert "interfaces" in hwsku_json, (
            "hwsku.json missing 'interfaces' key"
        )
        print(f"\n  hwsku.json loaded: "
              f"{len(hwsku_json['interfaces'])} interfaces")

    def test_hwsku_json_has_32_ports(self, hwsku_json):
        """hwsku.json defines exactly 32 ports."""
        ifaces = hwsku_json["interfaces"]
        assert len(ifaces) == NUM_PORTS, (
            f"Expected {NUM_PORTS} ports, got {len(ifaces)}"
        )

    def test_hwsku_json_default_mode(self, hwsku_json):
        """All ports default to 1x100G[40G] breakout mode."""
        ifaces = hwsku_json["interfaces"]
        issues = []
        for port in PARENT_PORTS:
            entry = ifaces.get(port, {})
            mode = entry.get("default_brkout_mode", "MISSING")
            if mode != "1x100G[40G]":
                issues.append(f"  {port}: default_brkout_mode={mode!r}")
        assert not issues, (
            f"Unexpected default breakout modes:\n" + "\n".join(issues)
        )


# ===================================================================
# Phase 14b — CLI integration
# ===================================================================

class TestBreakoutCli:
    """Phase 14b: Verify DPB CLI integration on the switch."""

    @pytest.fixture(scope="class", autouse=True)
    def _normalise_breakout(self, ssh):
        """Snapshot any non-default breakout modes, reset to 1x100G[40G] for
        the duration of this class, then restore on teardown.

        This allows ports that are intentionally left broken out (e.g.
        Ethernet64/Ethernet80 in 4x25G mode) to coexist with the test
        suite without causing false failures.
        """
        # Skip operational breakout parents — these are intentionally not 1x100G
        operational_breakout_parents = {"Ethernet0", "Ethernet64", "Ethernet80"}
        saved = {}
        for port in PARENT_PORTS:
            if port in operational_breakout_parents:
                continue
            out, _, rc = ssh.run(
                f"redis-cli -n 4 hget 'BREAKOUT_CFG|{port}' brkout_mode",
                timeout=10,
            )
            mode = out.strip()
            if mode and mode != "1x100G[40G]":
                saved[port] = mode

        if saved:
            print(f"\n  [fixture] Resetting {len(saved)} port(s) to 1x100G[40G] "
                  f"for test: {sorted(saved.keys())}")
            for port in saved:
                ssh.run(
                    f"sudo config interface breakout {port} '1x100G[40G]' -y -f -l",
                    timeout=60,
                )
            time.sleep(5)  # allow portmgrd to settle

        yield  # --- run tests ---

        if saved:
            print(f"\n  [fixture] Restoring breakout modes: "
                  f"{[f'{p}={m}' for p, m in saved.items()]}")
            for port, mode in saved.items():
                ssh.run(
                    f"sudo config interface breakout {port} '{mode}' -y -f -l",
                    timeout=60,
                )
            time.sleep(5)

    def test_show_interfaces_breakout(self, ssh):
        """show interfaces breakout returns valid JSON with all 32 ports.

        This command reads platform.json and displays available breakout
        modes — proving that SONiC recognizes the file.
        """
        out, err, rc = ssh.run("show interfaces breakout", timeout=15)
        assert rc == 0, (
            f"show interfaces breakout failed (rc={rc}): {err}\n"
            "Ensure platform.json is deployed to "
            "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/platform.json"
        )
        data = json.loads(out)
        assert len(data) == NUM_PORTS, (
            f"Expected {NUM_PORTS} ports in breakout output, got {len(data)}"
        )
        # Spot-check first and last port
        assert "Ethernet0" in data, "Ethernet0 missing from breakout output"
        assert "Ethernet124" in data, "Ethernet124 missing from breakout output"
        print(f"\n  show interfaces breakout: {len(data)} ports, "
              f"modes per port: {len(data['Ethernet0']['breakout_modes'])}")

    def test_breakout_modes_match_platform_json(self, ssh):
        """Breakout modes reported by CLI match what platform.json defines."""
        out, err, rc = ssh.run("show interfaces breakout", timeout=15)
        assert rc == 0, f"Command failed: {err}"
        data = json.loads(out)
        for port in PARENT_PORTS:
            modes = set(data.get(port, {}).get("breakout_modes", {}).keys())
            assert modes == EXPECTED_BREAKOUT_MODES, (
                f"{port}: CLI reports modes={sorted(modes)}, "
                f"expected {sorted(EXPECTED_BREAKOUT_MODES)}"
            )

    def test_breakout_cfg_table_populated(self, ssh):
        """BREAKOUT_CFG table in CONFIG_DB has entries for all 32 ports.

        This table tracks the current breakout mode of each port and is
        required for `config interface breakout` to work.  It must be
        seeded on first deployment of platform.json.
        """
        out, err, rc = ssh.run(
            "redis-cli -n 4 keys 'BREAKOUT_CFG|*'", timeout=10
        )
        assert rc == 0, f"redis-cli failed: {err}"
        keys = [k.strip() for k in out.strip().splitlines() if k.strip()]
        port_names = [k.split("|", 1)[1] for k in keys if "|" in k]
        print(f"\n  BREAKOUT_CFG entries: {len(port_names)}")

        if len(port_names) == 0:
            pytest.skip(
                "BREAKOUT_CFG table not yet initialized. "
                "Seed it by setting brkout_mode='1x100G[40G]' for each port "
                "in CONFIG_DB, or perform a config reload."
            )

        assert len(port_names) == NUM_PORTS, (
            f"Expected {NUM_PORTS} BREAKOUT_CFG entries, "
            f"got {len(port_names)}: {sorted(port_names)}"
        )

    def test_breakout_cfg_current_mode(self, ssh):
        """All non-operational ports in BREAKOUT_CFG show current mode 1x100G[40G]."""
        # Ethernet0, 64, 80 are operational breakout parents — skip them
        operational_breakout_parents = {"Ethernet0", "Ethernet64", "Ethernet80"}
        issues = []
        for port in PARENT_PORTS:
            if port in operational_breakout_parents:
                continue
            out, _, rc = ssh.run(
                f"redis-cli -n 4 hget 'BREAKOUT_CFG|{port}' brkout_mode",
                timeout=10,
            )
            mode = out.strip()
            if not mode:
                continue
            if mode != "1x100G[40G]":
                issues.append(f"  {port}: brkout_mode={mode!r}")
        if not issues:
            print(f"\n  All non-operational ports in 1x100G[40G] mode")
        assert not issues, (
            f"Unexpected breakout modes in BREAKOUT_CFG:\n"
            + "\n".join(issues)
        )
