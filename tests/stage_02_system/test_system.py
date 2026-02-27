"""Stage 02 — Software stack: kernel, NOS version, containers.

Captures the full software context of the running SONiC image.

Phase references: Phase 0 (topology discovery), Phase 1 (platform init), Phase 10 (build).
"""

import re
import pytest

# Containers always required on any SONiC system (including dev/partial boot)
REQUIRED_CONTAINERS = ["database", "pmon"]
# Containers expected on a fully-booted production switch (may be absent on dev systems)
OPTIONAL_CONTAINERS = ["syncd", "swss", "bgp", "teamd", "radv"]

# Minimum kernel major.minor we validated on
MIN_KERNEL = (6, 1)


# ------------------------------------------------------------------
# Kernel
# ------------------------------------------------------------------

def test_kernel_version(ssh):
    """uname -r reports a Linux kernel ≥ 6.1."""
    out, err, rc = ssh.run("uname -r")
    kernel = out.strip()
    print(f"\nKernel version: {kernel}")
    assert rc == 0, f"uname failed: {err}"
    assert kernel, "uname -r returned empty string"

    m = re.match(r"(\d+)\.(\d+)", kernel)
    assert m, f"Cannot parse kernel version from: {kernel}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= MIN_KERNEL, (
        f"Kernel {kernel} is below minimum {MIN_KERNEL[0]}.{MIN_KERNEL[1]}"
    )


def test_uname_full(ssh):
    """uname -a captures full OS/arch banner."""
    out, _, rc = ssh.run("uname -a")
    print(f"\nuname -a: {out.strip()}")
    assert rc == 0
    assert "Linux" in out
    assert "x86_64" in out or "amd64" in out.lower()


# ------------------------------------------------------------------
# SONiC NOS version
# ------------------------------------------------------------------

def test_sonic_version_file(ssh):
    """sonic_version.yml exists and is non-empty."""
    out, err, rc = ssh.run("cat /etc/sonic/sonic_version.yml")
    print(f"\n/etc/sonic/sonic_version.yml:\n{out}")
    assert rc == 0, f"Could not read sonic_version.yml: {err}"
    assert out.strip(), "sonic_version.yml is empty"
    assert "build_version" in out or "sonic_os_version" in out or "version" in out.lower(), (
        "sonic_version.yml does not contain expected version key"
    )


def test_show_version(ssh):
    """show version returns SONiC version block."""
    out, err, rc = ssh.run("show version")
    print(f"\nshow version:\n{out}")
    assert rc == 0, f"show version failed: {err}"
    assert "SONiC" in out or "sonic" in out.lower(), (
        "Expected 'SONiC' in show version output"
    )


# ------------------------------------------------------------------
# Hardware platform summary
# ------------------------------------------------------------------

def test_platform_summary(ssh):
    """show platform summary includes Wedge 100S platform name."""
    out, err, rc = ssh.run("show platform summary")
    print(f"\nshow platform summary:\n{out}")
    assert rc == 0, f"show platform summary failed: {err}"
    assert out.strip(), "show platform summary returned empty output"
    assert "wedge" in out.lower() or "accton" in out.lower(), (
        f"Expected 'wedge' or 'accton' in platform summary, got:\n{out}"
    )


def test_platform_summary_hwsku(ssh):
    """show platform summary reports the correct HW SKU."""
    out, _, _ = ssh.run("show platform summary")
    # e.g. HwSKU: Accton-Wedge100S-32X or x86_64-accton_wedge100s_32x-r0
    assert "wedge100" in out.lower() or "wedge100s" in out.lower(), (
        f"Expected wedge100 HwSKU in:\n{out}"
    )


def test_show_platform_syseeprom(ssh):
    """show platform syseeprom returns EEPROM data or a known 'not available' message.

    'show platform syseeprom' reads from STATE_DB (populated by chassis daemon).
    If the physical EEPROM is blank, the DB entry is absent and the command exits
    non-zero with "Failed to read system EEPROM info from DB".  Both outcomes are
    accepted; what matters is that the command itself is reachable.
    """
    out, err, rc = ssh.run("show platform syseeprom 2>&1")
    print(f"\nshow platform syseeprom (rc={rc}):\n{out}")
    combined = out + err
    # rc=0  → EEPROM data present in STATE_DB
    # rc!=0 → DB entry missing (blank EEPROM, sysd not yet populated it, etc.)
    known_not_available = (
        "Failed to read" in combined
        or "does not contain" in combined
        or "not programmed" in combined.lower()
    )
    assert rc == 0 or known_not_available, (
        f"show platform syseeprom failed unexpectedly (rc={rc}): {combined.strip()}"
    )
    if rc != 0:
        pytest.skip(
            f"show platform syseeprom: {combined.strip()} "
            "(EEPROM not programmed or STATE_DB entry absent)"
        )


# ------------------------------------------------------------------
# Containers
# ------------------------------------------------------------------

def test_docker_containers_running(ssh):
    """Required SONiC containers are Up; optional containers reported if absent."""
    out, err, rc = ssh.run(
        "docker ps --format '{{.Names}}\t{{.Status}}'"
    )
    print(f"\nDocker containers:\n{out}")
    assert rc == 0, f"docker ps failed: {err}"

    running = {}
    for line in out.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            running[parts[0].lower()] = parts[1]

    # Hard-required containers — must be Up on any SONiC system
    for cname in REQUIRED_CONTAINERS:
        found = any(cname in name for name in running)
        assert found, (
            f"Required container '{cname}' not found. "
            f"Running: {sorted(running.keys())}"
        )
        for name, status in running.items():
            if cname in name:
                assert "Up" in status, f"Container {name} is not Up: {status}"
                break

    # Optional containers — warn if absent (normal on dev/partial-boot systems)
    missing_optional = [c for c in OPTIONAL_CONTAINERS
                        if not any(c in name for name in running)]
    if missing_optional:
        print(f"\n  NOTE: optional containers not running: {missing_optional}")
        print("  (normal on dev systems — syncd/swss/bgp require a running ASIC)")


def test_docker_images(ssh):
    """docker images list is non-empty (images are present)."""
    out, _, rc = ssh.run("docker images --format '{{.Repository}}\t{{.Tag}}\t{{.Size}}'")
    print(f"\nDocker images (first 20):\n" + "\n".join(out.splitlines()[:20]))
    assert rc == 0
    assert out.strip(), "No docker images found"


def test_pmon_container_has_tty(ssh):
    """pmon container has access to /dev/ttyACM0 (BMC USB-CDC)."""
    out, err, rc = ssh.run(
        "docker exec pmon ls -la /dev/ttyACM0 2>/dev/null || echo MISSING"
    )
    print(f"\npmon /dev/ttyACM0: {out.strip()}")
    if "MISSING" in out:
        pytest.skip("pmon container not running or ttyACM0 not passed through")
    assert "ttyACM0" in out, f"ttyACM0 not visible inside pmon: {out}"


# ------------------------------------------------------------------
# Misc platform files
# ------------------------------------------------------------------

def test_platform_device_dir(ssh):
    """SONiC platform device directory exists with expected content."""
    dev_dir = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
    out, err, rc = ssh.run(f"ls {dev_dir}")
    print(f"\nDevice dir {dev_dir}:\n{out}")
    assert rc == 0, f"Platform device dir missing: {err}"
    assert out.strip(), f"Platform device dir is empty: {dev_dir}"


def test_sonic_platform_package(ssh):
    """sonic_platform Python package is importable on the target."""
    out, err, rc = ssh.run_python(
        "from sonic_platform.platform import Platform; print('OK')", timeout=20
    )
    print(f"\nsonic_platform import check: {out.strip()}")
    assert rc == 0, f"sonic_platform import failed: {err}"
    assert "OK" in out, f"Unexpected output: {out}"
