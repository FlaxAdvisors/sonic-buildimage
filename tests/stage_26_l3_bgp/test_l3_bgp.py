"""Stage 26 — L3 BGP container and routing verification.

Verifies that the BGP container (docker-fpm-frr) is running, FRR
daemons are healthy, and the switch is in LeafRouter mode.

These tests are non-destructive: they read state only.
"""

import pytest


class TestBGPContainer:

    def test_bgp_feature_enabled(self, ssh):
        """BGP feature must be in 'enabled' state."""
        out, _, rc = ssh.run("show feature status --json 2>/dev/null || "
                             "sonic-db-cli CONFIG_DB hget 'FEATURE|bgp' state",
                             timeout=15)
        assert rc == 0
        assert out.strip() == 'enabled' or 'enabled' in out

    def test_bgp_container_running(self, ssh):
        """docker-fpm-frr container must be Up."""
        out, _, rc = ssh.run("docker ps --filter name=bgp --format '{{.Status}}'",
                             timeout=15)
        assert rc == 0
        assert out.strip().startswith('Up'), f'BGP container not Up: {out!r}'

    def test_bgpd_running_in_container(self, ssh):
        """bgpd must be RUNNING inside the bgp container."""
        out, _, rc = ssh.run("docker exec bgp supervisorctl status bgpd",
                             timeout=15)
        assert rc == 0
        assert 'RUNNING' in out, f'bgpd not RUNNING: {out!r}'

    def test_bgpcfgd_running_in_container(self, ssh):
        """bgpcfgd must be RUNNING — it translates config_db to FRR."""
        out, _, rc = ssh.run("docker exec bgp supervisorctl status bgpcfgd",
                             timeout=15)
        assert rc == 0
        assert 'RUNNING' in out, f'bgpcfgd not RUNNING: {out!r}'

    def test_zebra_running_in_container(self, ssh):
        """zebra (FRR routing daemon) must be RUNNING."""
        out, _, rc = ssh.run("docker exec bgp supervisorctl status zebra",
                             timeout=15)
        assert rc == 0
        assert 'RUNNING' in out, f'zebra not RUNNING: {out!r}'

    def test_device_type_is_leafrouter(self, ssh):
        """DEVICE_METADATA.type must be LeafRouter for L3 mode."""
        out, _, rc = ssh.run(
            "sonic-db-cli CONFIG_DB hget 'DEVICE_METADATA|localhost' type",
            timeout=15
        )
        assert rc == 0
        assert out.strip() == 'LeafRouter', f'type is {out.strip()!r}, expected LeafRouter'

    def test_loopback0_has_ip(self, ssh):
        """Loopback0 must have an IP address (BGP router-id)."""
        out, _, rc = ssh.run("show ip interface Loopback0 2>/dev/null || "
                             "ip addr show Loopback0",
                             timeout=15)
        assert rc == 0
        assert '10.1.0.' in out, f'Loopback0 IP not found: {out!r}'
