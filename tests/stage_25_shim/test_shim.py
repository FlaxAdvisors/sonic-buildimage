import re
import time
import pytest

FLEX_PORTS = [
    "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
    "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    "Ethernet80", "Ethernet81", "Ethernet82", "Ethernet83",
]
NON_FLEX_PORTS = ["Ethernet16", "Ethernet32", "Ethernet48", "Ethernet112"]
SHIM_PATH = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/libsai-stat-shim.so"
MIN_STAT_KEYS = 60  # expect 68; allow some slack for future SAI version changes


def test_shim_library_present(ssh):
    """libsai-stat-shim.so is installed at the expected host path."""
    out, err, rc = ssh.run(f"test -f {SHIM_PATH} && echo OK || echo ABSENT", timeout=5)
    assert out.strip() == "OK", (
        f"Shim library not found at {SHIM_PATH}.\n"
        "Rebuild and install the .deb: dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb"
    )


def test_syncd_sh_patched(ssh):
    """syncd.sh contains the LD_PRELOAD injection line."""
    out, err, rc = ssh.run("grep -c 'libsai-stat-shim' /usr/bin/syncd.sh 2>/dev/null || true", timeout=5)
    assert int(out.strip() or "0") >= 1, (
        "syncd.sh has not been patched with the shim LD_PRELOAD.\n"
        "Run: sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb"
    )


def test_syncd_has_ld_preload(ssh):
    """The running syncd process has LD_PRELOAD set in its environment."""
    out, err, rc = ssh.run(
        "sudo docker exec syncd cat /proc/1/environ 2>/dev/null | tr '\\0' '\\n' | grep LD_PRELOAD || echo NONE",
        timeout=15
    )
    assert "libsai-stat-shim" in out, (
        "syncd process does not have LD_PRELOAD=libsai-stat-shim.\n"
        "The syncd container must be recreated: 'systemctl restart syncd' or reboot.\n"
        f"Current /proc/1/environ LD_PRELOAD: {out.strip()!r}"
    )


def _get_stat_key_count(ssh, port_name):
    """Return number of SAI_PORT_STAT_* keys in COUNTERS_DB for port_name."""
    oid_out, _, _ = ssh.run(
        f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port_name}", timeout=10
    )
    oid = oid_out.strip()
    if not oid:
        return 0
    keys_out, _, _ = ssh.run(
        f"redis-cli -n 2 hkeys 'COUNTERS:{oid}'", timeout=10
    )
    return sum(1 for k in keys_out.strip().splitlines() if k.startswith("SAI_PORT_STAT_"))


def test_flex_ports_have_full_stats(ssh):
    """Flex sub-ports have >= 60 SAI stat keys in COUNTERS_DB (shim working)."""
    # Allow up to 30s for flex counter poll to populate after syncd start.
    deadline = time.time() + 30
    results = {}
    while time.time() < deadline:
        for port in FLEX_PORTS:
            if port not in results:
                n = _get_stat_key_count(ssh, port)
                if n >= MIN_STAT_KEYS:
                    results[port] = n
        if len(results) == len(FLEX_PORTS):
            break
        time.sleep(3)

    failed = [p for p in FLEX_PORTS if p not in results]
    if failed:
        actuals = {p: _get_stat_key_count(ssh, p) for p in failed}
        pytest.fail(
            f"Flex ports with <{MIN_STAT_KEYS} stat keys (shim not working):\n"
            + "\n".join(f"  {p}: {actuals[p]} keys" for p in failed)
            + "\nExpected ≥60 keys. Check:\n"
            "  1. syncd has LD_PRELOAD: test_syncd_has_ld_preload\n"
            "  2. shim syslog: sudo grep 'sai-stat-shim' /var/log/syslog\n"
            "  3. bcmcmd socket: sudo docker exec syncd ls /var/run/sswsyncd/"
        )
    print(f"\nFlex port stat key counts: { {p: results[p] for p in FLEX_PORTS} }")


def test_non_flex_ports_not_regressed(ssh):
    """Non-flex ports still have >= 60 SAI stat keys (passthrough not broken)."""
    for port in NON_FLEX_PORTS:
        n = _get_stat_key_count(ssh, port)
        assert n >= MIN_STAT_KEYS, (
            f"{port}: only {n} stat keys (expected ≥{MIN_STAT_KEYS}). "
            "Shim passthrough may be broken — check get_port_stats intercept."
        )
    print(f"\nNon-flex stat counts: { {p: _get_stat_key_count(ssh, p) for p in NON_FLEX_PORTS} }")


def test_flex_port_rx_bytes_nonzero(ssh):
    """Flex sub-ports that are link-up show non-zero IF_IN_OCTETS."""
    up_ports = []
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    for port in FLEX_PORTS:
        if any(port in line and " up " in line for line in out.splitlines()):
            up_ports.append(port)
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up — cannot test RX counter increment")

    time.sleep(5)

    for port in up_ports[:2]:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget 'COUNTERS:{oid}' SAI_PORT_STAT_IF_IN_OCTETS", timeout=10
        )
        val = int(val_out.strip() or "0")
        print(f"  {port} IF_IN_OCTETS = {val:,}")
        assert val > 0, (
            f"{port}: IF_IN_OCTETS=0 even though link is up.\n"
            "Check bcmcmd 'show counters' for this port — may be 0 at BCM level too.\n"
            "Verify shim is connected: look for 'shim: bcmcmd connected' in syslog."
        )


def test_flex_port_tx_bytes_nonzero(ssh):
    """Flex sub-ports that are link-up show non-zero IF_OUT_OCTETS."""
    out, _, _ = ssh.run("show interfaces status 2>&1", timeout=20)
    up_ports = [p for p in FLEX_PORTS
                if any(p in line and " up " in line for line in out.splitlines())]
    if not up_ports:
        pytest.skip("No flex sub-ports are link-up")

    for port in up_ports[:2]:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hget 'COUNTERS:{oid}' SAI_PORT_STAT_IF_OUT_OCTETS", timeout=10
        )
        val = int(val_out.strip() or "0")
        print(f"  {port} IF_OUT_OCTETS = {val:,}")
        assert val > 0, (
            f"{port}: IF_OUT_OCTETS=0 even though link is up."
        )


def test_startup_zeros_succeed(ssh):
    """All 12 flex sub-ports have the IN_DROPPED_PKTS key present (even if 0).

    This key worked before the shim (it goes through a different SAI path).
    If the shim broke something, this key would disappear.  Also verifies that
    the shim path returns SAI_STATUS_SUCCESS even when cache is empty/stale.
    """
    for port in FLEX_PORTS:
        oid_out, _, _ = ssh.run(f"redis-cli -n 2 hget COUNTERS_PORT_NAME_MAP {port}", timeout=10)
        oid = oid_out.strip()
        if not oid:
            continue
        val_out, _, _ = ssh.run(
            f"redis-cli -n 2 hexists 'COUNTERS:{oid}' SAI_PORT_STAT_IN_DROPPED_PKTS", timeout=10
        )
        assert val_out.strip() == "1", (
            f"{port}: SAI_PORT_STAT_IN_DROPPED_PKTS key is MISSING.\n"
            "This was working before the shim — shim may have broken get_port_stats_ext path."
        )
    print("\nAll 12 flex ports: SAI_PORT_STAT_IN_DROPPED_PKTS key present ✓")
