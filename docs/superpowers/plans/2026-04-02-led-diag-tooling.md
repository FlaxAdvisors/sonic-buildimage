# LED Diagnostic Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained LED diagnostic tool (`wedge100s-led-diag.py`) and shared library (`wedge100s_ledup.py`) for the Wedge 100S that can set, read, and probe port LEDs via PCIe BAR2 `/dev/mem` — bypassing the broken bcmcmd/dsserve diag shell entirely.

**Architecture:** Two Python 3 files in the platform `utils/` directory. A shared library (`wedge100s_ledup.py`) provides BAR2 mmap register access, SOC file parsing, and CPLD control via BMC SSH. A CLI tool (`wedge100s-led-diag.py`) exposes status/set/probe commands. Both are installed to `/usr/bin/` on the target by the existing debian packaging (which copies all non-`.c` files from `utils/`). All BCM register access is via `/dev/mem` mmap of PCIe BAR2 — zero dependency on bcmcmd or dsserve.

**Tech Stack:** Python 3 stdlib only (`mmap`, `struct`, `os`, `argparse`, `json`, `subprocess`). BMC SSH over USB-CDC-Ethernet for CPLD access.

**Spec:** `docs/superpowers/specs/2026-04-02-led-diag-tooling-design.md`

**Existing code to build on:**
- `utils/read_ledup_mmap.py` (repo root) — working BAR2 mmap, `find_bcm_bar2()`, DATA_RAM decode
- `device/accton/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc` — LED bytecode + PORT_ORDER_REMAP
- `platform/.../wedge100s-32x/utils/wedge100s-bmc-daemon.c` — BMC daemon with `.set` dispatch

**Target hardware:** `ssh admin@192.168.88.12` (SONiC), `ssh root@192.168.88.13` (BMC)

---

## File Structure

| File | Location | Responsibility |
|------|----------|---------------|
| `wedge100s_ledup.py` | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/` | Shared library: BAR2 mmap access, register R/W, SOC file parsing, CPLD access, port mapping |
| `wedge100s-led-diag.py` | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/` | CLI tool: argparse, status/set/probe subcommands |
| `test_wedge100s_ledup.py` | `tests/` | Unit tests for SOC parser, port mapping, offset calculations (runs on dev host) |
| `wedge100s-bmc-daemon.c` | `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/` | MODIFY: add `cpld_led_ctrl.set` dispatch entry |

Both Python files are installed to `/usr/bin/` on the target by the existing `debian/rules` (copies all non-`.c` files from `utils/`). The diag tool imports the library using `sys.path` manipulation.

---

## Task 1: Shared Library — Constants and SOC File Parser

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py`
- Create: `tests/test_wedge100s_ledup.py`
- Read: `device/accton/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc`

This task builds the pure-logic portion of the shared library: register offset constants, SOC file bytecode parser, and front-panel-to-DATA_RAM port mapping. All testable on the dev host without hardware.

- [ ] **Step 1: Write failing tests for SOC bytecode parser**

```python
# tests/test_wedge100s_ledup.py
"""Unit tests for wedge100s_ledup.py — SOC parser, constants, port mapping.

Runs on dev host (no hardware required). Tests pure logic only.
"""
import os
import sys
import pytest

# The library lives in the platform utils directory
UTILS_DIR = os.path.join(
    os.path.dirname(__file__), "..",
    "platform", "broadcom", "sonic-platform-modules-accton",
    "wedge100s-32x", "utils",
)
sys.path.insert(0, UTILS_DIR)
import wedge100s_ledup as ledup

SOC_PATH = os.path.join(
    os.path.dirname(__file__), "..",
    "device", "accton", "x86_64-accton_wedge100s_32x-r0",
    "led_proc_init.soc",
)


class TestSocBytecodeParser:
    def test_parses_two_processors(self):
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        assert 0 in result and 1 in result

    def test_bytecode_length_le_256(self):
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        for proc, bytecodes in result.items():
            assert len(bytecodes) <= 256, f"LEDUP{proc} bytecode too long"

    def test_bytecodes_are_ints_0_to_255(self):
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        for proc, bytecodes in result.items():
            for b in bytecodes:
                assert 0 <= b <= 255

    def test_both_processors_have_identical_bytecode(self):
        """AS7712/Wedge100S: both processors run the same program."""
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        assert result[0] == result[1]

    def test_first_bytes_match_soc_file(self):
        """Verify against known first 4 bytes from led_proc_init.soc."""
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        assert result[0][:4] == [0x02, 0xFD, 0x42, 0x80]
```

- [ ] **Step 2: Write failing tests for port remap parser**

Append to `tests/test_wedge100s_ledup.py`:

```python
class TestSocRemapParser:
    def test_returns_32_port_mapping(self):
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert len(mapping) == 32

    def test_fp1_maps_to_data_ram_29(self):
        """FP1/Ethernet0 → LED port 29 (from SOC file comments)."""
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert mapping[1] == 29

    def test_fp6_maps_to_data_ram_0(self):
        """FP6/Ethernet20 → LED port 0."""
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert mapping[6] == 0

    def test_fp32_maps_to_data_ram_26(self):
        """FP32/Ethernet124 → LED port 26."""
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert mapping[32] == 26

    def test_all_indices_unique(self):
        mapping = ledup.parse_soc_remap(SOC_PATH)
        values = list(mapping.values())
        assert len(values) == len(set(values)), "Duplicate DATA_RAM indices"

    def test_all_indices_in_range(self):
        mapping = ledup.parse_soc_remap(SOC_PATH)
        for fp, idx in mapping.items():
            assert 0 <= idx <= 31, f"FP{fp} maps to out-of-range index {idx}"
```

- [ ] **Step 3: Write failing tests for register offset constants**

Append to `tests/test_wedge100s_ledup.py`:

```python
class TestConstants:
    def test_ledup0_offsets(self):
        assert ledup.LEDUP0_CTRL == 0x34000
        assert ledup.LEDUP0_PROGRAM_RAM_BASE == 0x34100
        assert ledup.LEDUP0_DATA_RAM_BASE == 0x34800

    def test_ledup1_offsets(self):
        assert ledup.LEDUP1_CTRL == 0x34400
        assert ledup.LEDUP1_PROGRAM_RAM_BASE == 0x34500
        assert ledup.LEDUP1_DATA_RAM_BASE == 0x34C00

    def test_program_ram_offset_helper(self):
        assert ledup.program_ram_offset(0, 0) == 0x34100
        assert ledup.program_ram_offset(0, 10) == 0x34100 + 40
        assert ledup.program_ram_offset(1, 0) == 0x34500

    def test_data_ram_offset_helper(self):
        assert ledup.data_ram_offset(0, 0) == 0x34800
        assert ledup.data_ram_offset(0, 29) == 0x34800 + 29 * 4
        assert ledup.data_ram_offset(1, 0) == 0x34C00
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd /export/sonic/sonic-buildimage.claude && python3 -m pytest tests/test_wedge100s_ledup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wedge100s_ledup'`

- [ ] **Step 5: Implement constants and parsers in wedge100s_ledup.py**

```python
#!/usr/bin/env python3
"""wedge100s_ledup.py — BCM56960 LEDUP register access and SOC file parsing.

Shared library for LED diagnostic tooling on the Accton Wedge 100S-32X.
All BCM register access via PCIe BAR2 /dev/mem mmap (no bcmcmd dependency).
CPLD access via BMC SSH.

Requires root for /dev/mem access.
"""

import mmap
import os
import re
import struct
import subprocess

# ── BCM56960 LEDUP register offsets within PCIe BAR2 ──────────────────────

LEDUP0_CTRL = 0x34000
LEDUP0_STATUS = 0x34004
LEDUP0_PROGRAM_RAM_BASE = 0x34100   # + 4*n, n=0..255
LEDUP0_DATA_RAM_BASE = 0x34800      # + 4*n, n=0..255
LEDUP1_CTRL = 0x34400
LEDUP1_STATUS = 0x34404
LEDUP1_PROGRAM_RAM_BASE = 0x34500
LEDUP1_DATA_RAM_BASE = 0x34C00

NUM_PORTS = 32
PROGRAM_RAM_SIZE = 256

# Base offsets indexed by processor number
_CTRL_BASES = {0: LEDUP0_CTRL, 1: LEDUP1_CTRL}
_STATUS_BASES = {0: LEDUP0_STATUS, 1: LEDUP1_STATUS}
_PROG_BASES = {0: LEDUP0_PROGRAM_RAM_BASE, 1: LEDUP1_PROGRAM_RAM_BASE}
_DATA_BASES = {0: LEDUP0_DATA_RAM_BASE, 1: LEDUP1_DATA_RAM_BASE}

# BCM56960 PCI IDs
BCM_VID = "0x14e4"
BCM_DID = "0xb960"

# DATA_RAM per-port status bit positions
BIT_LINK = 0x80
BIT_FC = 0x40
BIT_FD = 0x20
BIT_SPEED_MASK = 0x18
BIT_COL = 0x04
BIT_TX = 0x02
BIT_RX = 0x01

SPEED_NAMES = {0: "10M", 1: "100M", 2: "1G", 3: "10G+"}


def program_ram_offset(proc, index):
    """Return BAR2 offset for PROGRAM_RAM[index] on processor proc."""
    return _PROG_BASES[proc] + 4 * index


def data_ram_offset(proc, index):
    """Return BAR2 offset for DATA_RAM[index] on processor proc."""
    return _DATA_BASES[proc] + 4 * index


# ── SOC file parsing ──────────────────────────────────────────────────────

def parse_soc_bytecodes(soc_path):
    """Parse led_proc_init.soc, return {proc_num: [byte, byte, ...]}."""
    result = {}
    with open(soc_path) as f:
        for line in f:
            m = re.match(r'led\s+(\d+)\s+prog\s+(.*)', line.strip())
            if m:
                proc = int(m.group(1))
                hexbytes = m.group(2).split()
                result[proc] = [int(b, 16) for b in hexbytes]
    return result


def parse_soc_remap(soc_path):
    """Parse PORT_ORDER_REMAP lines from SOC file.

    Returns dict mapping front-panel port (1-32) to DATA_RAM index.
    Uses LEDUP0 remap only (LEDUP0 and LEDUP1 are identical for positions 0-31).
    """
    # Build position → DATA_RAM index from LEDUP0 REMAP registers
    pos_to_index = {}
    with open(soc_path) as f:
        for line in f:
            if not line.strip().startswith('m CMIC_LEDUP0_PORT_ORDER_REMAP_'):
                continue
            for m in re.finditer(r'REMAP_PORT_(\d+)=(\d+)', line):
                pos = int(m.group(1))
                idx = int(m.group(2))
                if pos < NUM_PORTS:
                    pos_to_index[pos] = idx

    # Front panel port N = position N-1
    return {fp: pos_to_index[fp - 1] for fp in range(1, NUM_PORTS + 1)}


def decode_data_ram(val):
    """Decode a DATA_RAM byte into human-readable string."""
    val = val & 0xFF
    if val == 0:
        return "(dark)"
    parts = []
    if val & BIT_LINK:
        parts.append("Link")
    if val & BIT_FC:
        parts.append("FC")
    if val & BIT_FD:
        parts.append("FD")
    parts.append(SPEED_NAMES[(val >> 3) & 3])
    if val & BIT_COL:
        parts.append("Col")
    if val & BIT_TX:
        parts.append("TX")
    if val & BIT_RX:
        parts.append("RX")
    return " ".join(parts)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /export/sonic/sonic-buildimage.claude && python3 -m pytest tests/test_wedge100s_ledup.py -v`
Expected: All 16 tests PASS

- [ ] **Step 7: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py tests/test_wedge100s_ledup.py
git commit -m "feat(led-diag): add shared library constants and SOC parser with tests"
```

---

## Task 2: Shared Library — BAR2 Access Class

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py`
- Reference: `utils/read_ledup_mmap.py` (proven working BAR2 code)

Adds the `LedupAccess` class: PCI device discovery, `/dev/mem` mmap, register read/write, and convenience methods for CTRL, DATA_RAM, and PROGRAM_RAM. Tested on hardware via SSH.

- [ ] **Step 1: Add LedupAccess class to wedge100s_ledup.py**

Append to `wedge100s_ledup.py` after the `decode_data_ram` function:

```python
# ── BAR2 memory-mapped register access ────────────────────────────────────

def find_bcm_bar2():
    """Auto-discover BCM56960 PCIe BAR2 address and size.

    Scans /sys/bus/pci/devices/ for vendor 14e4, device b960.
    Returns (bar2_addr, bar2_size) or raises RuntimeError.
    """
    pci_dir = "/sys/bus/pci/devices"
    for dev in os.listdir(pci_dir):
        devpath = os.path.join(pci_dir, dev)
        try:
            vid = open(os.path.join(devpath, "vendor")).read().strip()
            did = open(os.path.join(devpath, "device")).read().strip()
        except OSError:
            continue
        if vid == BCM_VID and did == BCM_DID:
            lines = open(os.path.join(devpath, "resource")).read().strip().split("\n")
            parts = lines[2].split()  # BAR2 is index 2
            start = int(parts[0], 16)
            end = int(parts[1], 16)
            if start == 0:
                continue
            return start, end - start + 1
    raise RuntimeError("BCM56960 (14e4:b960) BAR2 not found in /sys/bus/pci/devices")


class LedupAccess:
    """Memory-mapped access to BCM56960 LEDUP registers via /dev/mem.

    Usage:
        with LedupAccess() as led:
            ctrl = led.read_ctrl(0)
            led.write_data_ram(0, 29, 0x80)
    """

    def __init__(self):
        self._bar_addr, self._bar_size = find_bcm_bar2()
        self._fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self._mm = mmap.mmap(
            self._fd, self._bar_size, mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=self._bar_addr,
        )

    def close(self):
        if self._mm:
            self._mm.close()
            self._mm = None
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def bar2_info(self):
        """Return (bar2_addr, bar2_size) for display."""
        return self._bar_addr, self._bar_size

    def reg_read32(self, offset):
        """Read a 32-bit register at the given BAR2 offset."""
        self._mm.seek(offset)
        return struct.unpack("<I", self._mm.read(4))[0]

    def reg_write32(self, offset, value):
        """Write a 32-bit register at the given BAR2 offset."""
        self._mm.seek(offset)
        self._mm.write(struct.pack("<I", value & 0xFFFFFFFF))

    def read_ctrl(self, proc):
        return self.reg_read32(_CTRL_BASES[proc])

    def write_ctrl(self, proc, value):
        self.reg_write32(_CTRL_BASES[proc], value)

    def read_status(self, proc):
        return self.reg_read32(_STATUS_BASES[proc])

    def read_data_ram(self, proc, index):
        return self.reg_read32(data_ram_offset(proc, index)) & 0xFF

    def write_data_ram(self, proc, index, value):
        self.reg_write32(data_ram_offset(proc, index), value & 0xFF)

    def read_program_ram(self, proc, index):
        return self.reg_read32(program_ram_offset(proc, index)) & 0xFF

    def write_program_ram(self, proc, index, value):
        self.reg_write32(program_ram_offset(proc, index), value & 0xFF)

    def load_bytecode(self, proc, bytecodes):
        """Write bytecode list to PROGRAM_RAM. Pads to 256 with zeros."""
        for i in range(PROGRAM_RAM_SIZE):
            val = bytecodes[i] if i < len(bytecodes) else 0
            self.write_program_ram(proc, i, val)

    def verify_bytecode(self, proc, bytecodes):
        """Read back PROGRAM_RAM and compare. Returns (ok, first_mismatch_index)."""
        for i in range(len(bytecodes)):
            actual = self.read_program_ram(proc, i)
            if actual != bytecodes[i]:
                return False, i
        return True, -1

    def zero_data_ram(self, proc, count=NUM_PORTS):
        """Write zero to first `count` DATA_RAM entries."""
        for i in range(count):
            self.write_data_ram(proc, i, 0)
```

- [ ] **Step 2: Deploy and test BAR2 access on hardware**

Deploy the library to the target and run a quick smoke test:

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py admin@192.168.88.12:~

ssh admin@192.168.88.12 'sudo python3 -c "
import sys; sys.path.insert(0, \".\")
import wedge100s_ledup as ledup
with ledup.LedupAccess() as led:
    addr, size = led.bar2_info()
    print(\"BAR2: 0x%x size=0x%x\" % (addr, size))
    for proc in (0, 1):
        ctrl = led.read_ctrl(proc)
        status = led.read_status(proc)
        print(\"LEDUP%d CTRL=0x%08x STATUS=0x%08x\" % (proc, ctrl, status))
        nz = sum(1 for i in range(32) if led.read_data_ram(proc, i) != 0)
        print(\"  DATA_RAM non-zero entries: %d/32\" % nz)
print(\"PASS: BAR2 access works\")
"'
```

Expected: Prints BAR2 address (0xfb000000), CTRL values (likely 0x00000000 since ledinit failed), and DATA_RAM summary.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py
git commit -m "feat(led-diag): add BAR2 mmap access class (LedupAccess)"
```

---

## Task 3: Shared Library — CPLD Access

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py`
- Reference: `platform/.../utils/wedge100s-bmc-daemon.c` (SSH connection pattern)

Adds the `CpldAccess` class for reading/writing BMC SYSCPLD registers via SSH. Auto-detects whether the BMC daemon is running (use `.set` file dispatch) or stopped (use direct SSH).

- [ ] **Step 1: Add CpldAccess class to wedge100s_ledup.py**

Append after the `LedupAccess` class:

```python
# ── CPLD access via BMC SSH ───────────────────────────────────────────────

# BMC SYSCPLD sysfs paths (i2c-12, addr 0x31)
CPLD_SYSFS_DIR = "/sys/bus/i2c/devices/12-0031"
CPLD_LED_CTRL_REG = "0x3c"      # register address for i2cget
CPLD_LED_COLOR_REG = "0x3d"     # test color selector

# CPLD 0x3c bit definitions
CPLD_TEST_MODE_EN = 0x80        # bit 7
CPLD_TEST_BLINK_EN = 0x40       # bit 6
CPLD_TH_LED_STEAM_MASK = 0x30   # bits 5:4
CPLD_WALK_TEST_EN = 0x08        # bit 3
CPLD_TH_LED_EN = 0x02           # bit 1 — BCM passthrough enable
CPLD_TH_LED_CLR = 0x01          # bit 0

# Preset CPLD 0x3c values
CPLD_RAINBOW = 0xE0             # test mode on, blink on, steam=2
CPLD_PASSTHROUGH = 0x02         # passthrough only
CPLD_ALL_OFF = 0x00             # everything disabled

BMC_KEY = "/etc/sonic/wedge100s-bmc-key"
BMC_HOST = "root@fe80::ff:fe00:1%usb0"
RUN_DIR = "/run/wedge100s"


class CpldAccess:
    """Read/write BMC SYSCPLD registers via SSH.

    Auto-detects daemon state:
    - Daemon running: writes go via .set file dispatch (daemon handles SSH)
    - Daemon stopped: direct SSH to BMC
    """

    def __init__(self, bmc_host=BMC_HOST, bmc_key=BMC_KEY):
        self._bmc_host = bmc_host
        self._bmc_key = bmc_key

    def _ssh_cmd(self, bmc_command):
        """Run a command on the BMC via SSH. Returns stdout string."""
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", "-i", self._bmc_key,
            self._bmc_host, bmc_command,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.stdout.strip()

    def daemon_is_active(self):
        """Check if wedge100s-bmc-daemon is running."""
        result = subprocess.run(
            ["systemctl", "is-active", "wedge100s-bmc-daemon"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() == "active"

    def read_cpld_reg(self, reg_addr):
        """Read a CPLD register (hex address string like '0x3c'). Returns int."""
        output = self._ssh_cmd(
            "i2cget -f -y 12 0x31 %s" % reg_addr
        )
        return int(output, 0)

    def write_cpld_reg(self, reg_addr, value):
        """Write a CPLD register via BMC SSH."""
        self._ssh_cmd(
            "i2cset -f -y 12 0x31 %s 0x%02x" % (reg_addr, value)
        )

    def read_led_ctrl(self):
        """Read CPLD register 0x3c (LED control). Returns int."""
        return self.read_cpld_reg(CPLD_LED_CTRL_REG)

    def write_led_ctrl(self, value):
        """Write CPLD register 0x3c."""
        self.write_cpld_reg(CPLD_LED_CTRL_REG, value)

    def read_led_color(self):
        """Read CPLD register 0x3d (test color selector). Returns int."""
        return self.read_cpld_reg(CPLD_LED_COLOR_REG)

    def write_led_color(self, value):
        """Write CPLD register 0x3d."""
        self.write_cpld_reg(CPLD_LED_COLOR_REG, value)

    def decode_led_ctrl(self, val):
        """Decode 0x3c register into human-readable dict."""
        return {
            "raw": "0x%02x" % val,
            "led_test_mode_en": bool(val & CPLD_TEST_MODE_EN),
            "led_test_blink_en": bool(val & CPLD_TEST_BLINK_EN),
            "th_led_steam": (val & CPLD_TH_LED_STEAM_MASK) >> 4,
            "walk_test_en": bool(val & CPLD_WALK_TEST_EN),
            "th_led_en": bool(val & CPLD_TH_LED_EN),
            "th_led_clr": bool(val & CPLD_TH_LED_CLR),
        }
```

- [ ] **Step 2: Deploy and test CPLD access on hardware**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py admin@192.168.88.12:~

ssh admin@192.168.88.12 'sudo python3 -c "
import sys; sys.path.insert(0, \".\")
import wedge100s_ledup as ledup
cpld = ledup.CpldAccess()
val = cpld.read_led_ctrl()
info = cpld.decode_led_ctrl(val)
print(\"CPLD 0x3c:\", info)
assert info[\"th_led_en\"], \"Expected passthrough enabled\"
print(\"PASS: CPLD access works\")
"'
```

Expected: Shows `raw: 0x02`, `th_led_en: True`, all test modes off.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py
git commit -m "feat(led-diag): add CpldAccess class for BMC SYSCPLD register R/W"
```

---

## Task 4: CLI Tool — Skeleton and Status Command

**Files:**
- Create: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py`

The diagnostic CLI tool. This task builds the argparse skeleton and the `status` command, which dumps the complete LED pipeline state: CPLD 0x3c, LEDUP CTRL/STATUS, and DATA_RAM summary for both processors.

- [ ] **Step 1: Create the CLI tool with status command**

```python
#!/usr/bin/env python3
"""wedge100s-led-diag.py — LED diagnostic and control tool for Wedge 100S-32X.

Requires root. All ASIC access via PCIe BAR2 /dev/mem (no bcmcmd dependency).
CPLD access via BMC SSH.

Usage:
    wedge100s-led-diag.py status
    wedge100s-led-diag.py set rainbow
    wedge100s-led-diag.py set passthrough
    wedge100s-led-diag.py set all-off
    wedge100s-led-diag.py set color <ledup0|ledup1|both|off>
    wedge100s-led-diag.py set port <1-32> <ledup0|ledup1|both|off>
    wedge100s-led-diag.py probe
"""

import argparse
import json
import os
import sys
import time

# Import shared library from same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wedge100s_ledup as ledup

SOC_PATH_DEVICE = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc"
SOC_PATH_HWSKU = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/led_proc_init.soc"
PROBE_RESULTS_PATH = os.path.join(ledup.RUN_DIR, "led_probe_results.json")


def find_soc_path():
    """Find led_proc_init.soc on the target filesystem."""
    for p in (SOC_PATH_DEVICE, SOC_PATH_HWSKU):
        if os.path.exists(p):
            return p
    # Fallback: search device directory
    base = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
    for root, dirs, files in os.walk(base):
        if "led_proc_init.soc" in files:
            return os.path.join(root, "led_proc_init.soc")
    return None


def cmd_status(args):
    """Dump complete LED pipeline state."""
    cpld = ledup.CpldAccess()

    # CPLD state
    print("=== CPLD LED Control (0x3c) ===")
    try:
        val = cpld.read_led_ctrl()
        info = cpld.decode_led_ctrl(val)
        for k, v in info.items():
            print("  %-20s %s" % (k, v))
    except Exception as e:
        print("  ERROR reading CPLD: %s" % e)

    # LEDUP state
    with ledup.LedupAccess() as led:
        bar_addr, bar_size = led.bar2_info()
        print("\n=== BCM56960 BAR2: 0x%x (size 0x%x) ===" % (bar_addr, bar_size))

        for proc in (0, 1):
            ctrl = led.read_ctrl(proc)
            status = led.read_status(proc)
            print("\n--- LEDUP%d ---" % proc)
            print("  CTRL:   0x%08x  (LEDUP_EN=%d)" % (ctrl, ctrl & 1))
            print("  STATUS: 0x%08x" % status)

            # Check if any bytecode is loaded
            prog_nonzero = sum(
                1 for i in range(ledup.PROGRAM_RAM_SIZE)
                if led.read_program_ram(proc, i) != 0
            )
            print("  PROGRAM_RAM: %d/256 non-zero entries" % prog_nonzero)

            # DATA_RAM summary
            print("  DATA_RAM[0..31]:")
            for i in range(ledup.NUM_PORTS):
                val = led.read_data_ram(proc, i)
                if val != 0:
                    print("    [%2d] 0x%02x  %s" % (i, val, ledup.decode_data_ram(val)))
            nz = sum(1 for i in range(ledup.NUM_PORTS) if led.read_data_ram(proc, i) != 0)
            if nz == 0:
                print("    (all zero)")


def main():
    parser = argparse.ArgumentParser(
        description="Wedge 100S-32X LED diagnostic and control tool",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Dump CPLD + LEDUP state")

    set_parser = sub.add_parser("set", help="Set LED mode")
    set_sub = set_parser.add_subparsers(dest="mode")
    set_sub.add_parser("rainbow", help="CPLD test mode (rainbow)")
    set_sub.add_parser("passthrough", help="CPLD passthrough + load bytecode")
    set_sub.add_parser("all-off", help="Disable all LEDs")
    color_parser = set_sub.add_parser("color", help="Software-drive all ports")
    color_parser.add_argument("color", choices=["ledup0", "ledup1", "both", "off"])
    port_parser = set_sub.add_parser("port", help="Software-drive single port")
    port_parser.add_argument("port_num", type=int, choices=range(1, 33), metavar="1-32")
    port_parser.add_argument("color", choices=["ledup0", "ledup1", "both", "off"])

    sub.add_parser("probe", help="Discover LED color mapping")

    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: must run as root", file=sys.stderr)
        sys.exit(1)

    if args.command == "status":
        cmd_status(args)
    elif args.command == "set":
        if args.mode == "rainbow":
            cmd_set_rainbow(args)
        elif args.mode == "passthrough":
            cmd_set_passthrough(args)
        elif args.mode == "all-off":
            cmd_set_all_off(args)
        elif args.mode == "color":
            cmd_set_color(args)
        elif args.mode == "port":
            cmd_set_port(args)
        else:
            set_parser.print_help()
    elif args.command == "probe":
        cmd_probe(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Deploy and test status command on hardware**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    admin@192.168.88.12:~

ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py status'
```

Expected: Full dump of CPLD 0x3c (should show passthrough), LEDUP0/1 CTRL (likely 0x00), PROGRAM_RAM (0 entries), DATA_RAM (likely all zero).

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py
git commit -m "feat(led-diag): add CLI tool skeleton with status command"
```

---

## Task 5: Set Rainbow and Set All-Off Commands

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py`

CPLD-only commands — no BCM involvement. `set rainbow` enables CPLD test mode (0xE0), `set all-off` disables everything (CPLD 0x00 + LEDUP disabled + DATA_RAM zeroed).

- [ ] **Step 1: Implement set rainbow and set all-off**

Add these functions to `wedge100s-led-diag.py` before the `main()` function:

```python
def cmd_set_rainbow(args):
    """Set CPLD to test mode — drives rainbow pattern from CPLD, no BCM."""
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_RAINBOW)
    readback = cpld.read_led_ctrl()
    if readback == ledup.CPLD_RAINBOW:
        print("PASS: CPLD 0x3c = 0x%02x (rainbow test mode)" % readback)
    else:
        print("FAIL: wrote 0x%02x, read back 0x%02x" % (ledup.CPLD_RAINBOW, readback))
        sys.exit(1)


def cmd_set_all_off(args):
    """Disable all port LEDs: CPLD off + LEDUP disabled + DATA_RAM zeroed."""
    # Disable CPLD passthrough and test modes
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_ALL_OFF)
    readback = cpld.read_led_ctrl()
    print("CPLD 0x3c: wrote 0x%02x, read 0x%02x — %s" % (
        ledup.CPLD_ALL_OFF, readback,
        "PASS" if readback == ledup.CPLD_ALL_OFF else "FAIL"))

    # Disable LEDUP processors and zero DATA_RAM
    with ledup.LedupAccess() as led:
        for proc in (0, 1):
            led.write_ctrl(proc, 0x00000000)
            led.zero_data_ram(proc)
            ctrl = led.read_ctrl(proc)
            print("LEDUP%d CTRL: 0x%08x — %s" % (
                proc, ctrl, "PASS" if ctrl == 0 else "FAIL"))

    print("All port LEDs disabled.")
```

- [ ] **Step 2: Test on hardware**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    admin@192.168.88.12:~

# Test rainbow — should show physical rainbow on front panel
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set rainbow'

# Pause to visually confirm rainbow (or ask user)
# Test all-off — should extinguish all port LEDs
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set all-off'

# Restore passthrough
ssh admin@192.168.88.12 'sudo python3 -c "
import sys; sys.path.insert(0, \".\")
import wedge100s_ledup as ledup
cpld = ledup.CpldAccess()
cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
print(\"Restored: 0x%02x\" % cpld.read_led_ctrl())
"'
```

Expected: `set rainbow` prints PASS and LEDs show rainbow. `set all-off` prints PASS for CPLD and both LEDUP processors.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py
git commit -m "feat(led-diag): add set rainbow and set all-off commands"
```

---

## Task 6: Bytecode Loading and LEDUP Enable

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py`

This is the critical task: load LED bytecode from `led_proc_init.soc` into PROGRAM_RAM via `/dev/mem`, enable the LEDUP processors, and verify the scan chain is running. This replaces what `ledinit` was supposed to do via bcmcmd.

The CTRL register bit layout is partially unknown. We know from the investigation that the working state had `LEDUP_EN=1, SCAN_START_DELAY=0x2a, INTRA_PORT_DELAY=4`. This task includes empirical discovery of the CTRL bit packing by reading the register when it has a known-good value, or by trying plausible values.

- [ ] **Step 1: Add helper function to load SOC file and enable LEDUP**

Add to `wedge100s-led-diag.py` before `cmd_set_rainbow`:

```python
def load_and_enable_ledup(led, soc_path):
    """Load bytecode from SOC file into PROGRAM_RAM, enable LEDUP processors.

    Args:
        led: LedupAccess instance (already open)
        soc_path: path to led_proc_init.soc

    Returns True on success, False on failure.
    """
    bytecodes = ledup.parse_soc_bytecodes(soc_path)
    if not bytecodes:
        print("ERROR: no bytecode found in %s" % soc_path)
        return False

    ok = True
    for proc in sorted(bytecodes.keys()):
        bc = bytecodes[proc]
        print("Loading LEDUP%d: %d bytes..." % (proc, len(bc)), end=" ")
        led.load_bytecode(proc, bc)
        verified, mismatch = led.verify_bytecode(proc, bc)
        if verified:
            print("verified OK")
        else:
            print("FAIL at index %d (wrote 0x%02x, read 0x%02x)" % (
                mismatch, bc[mismatch], led.read_program_ram(proc, mismatch)))
            ok = False

    if not ok:
        return False

    # Enable LEDUP processors.
    # CTRL register bit layout (from BCM56960 investigation):
    #   Bit 0: LEDUP_EN
    #   The exact bit positions for SCAN_START_DELAY and INTRA_PORT_DELAY
    #   are discovered empirically. Start with just LEDUP_EN=1 (value 0x01).
    #   If that doesn't work, try the full value from the investigation.
    #
    # Strategy: write CTRL=1, check STATUS for RUNNING bit. If not running
    # after a brief delay, try larger CTRL values with timing fields.
    ctrl_candidates = [
        0x00000001,          # minimal: just LEDUP_EN
        0x00002A41,          # LEDUP_EN + SCAN_START_DELAY=0x2a<<8 + INTRA_PORT_DELAY=4<<4
        0x0004_2A01,         # LEDUP_EN + INTRA_PORT_DELAY=4<<16 + SCAN_START_DELAY=0x2a<<8
    ]

    for proc in sorted(bytecodes.keys()):
        enabled = False
        for ctrl_val in ctrl_candidates:
            led.write_ctrl(proc, ctrl_val)
            time.sleep(0.05)
            status = led.read_status(proc)
            readback = led.read_ctrl(proc)
            if readback & 1:  # LEDUP_EN bit is set
                print("LEDUP%d: CTRL=0x%08x STATUS=0x%08x — enabled" % (
                    proc, readback, status))
                enabled = True
                break
            print("LEDUP%d: CTRL=0x%08x (tried 0x%08x, readback 0x%08x)" % (
                proc, readback, ctrl_val, readback))

        if not enabled:
            print("WARNING: LEDUP%d could not be enabled — check CTRL register format" % proc)
            ok = False

    return ok
```

- [ ] **Step 2: Deploy and test bytecode loading on hardware**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    admin@192.168.88.12:~

# Ensure CPLD is in passthrough mode
ssh admin@192.168.88.12 'sudo python3 -c "
import sys; sys.path.insert(0, \".\")
import wedge100s_ledup as ledup
cpld = ledup.CpldAccess()
cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
print(\"CPLD 0x3c = 0x%02x\" % cpld.read_led_ctrl())
"'

# Load bytecode and enable LEDUP
ssh admin@192.168.88.12 'sudo python3 -c "
import sys; sys.path.insert(0, \".\")
import wedge100s_ledup as ledup
soc = \"/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc\"
# Try alternate path if first doesn't exist
import os
if not os.path.exists(soc):
    for root, dirs, files in os.walk(\"/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0\"):
        if \"led_proc_init.soc\" in files:
            soc = os.path.join(root, \"led_proc_init.soc\")
            break
print(\"SOC path:\", soc)

# Import the diag tool to use load_and_enable_ledup
sys.path.insert(0, \".\")
exec(open(\"wedge100s-led-diag.py\").read().split(\"if __name__\")[0])
with ledup.LedupAccess() as led:
    ok = load_and_enable_ledup(led, soc)
    print(\"Result:\", \"SUCCESS\" if ok else \"NEEDS INVESTIGATION\")
    # Check status
    for proc in (0, 1):
        print(\"LEDUP%d CTRL=0x%08x STATUS=0x%08x\" % (
            proc, led.read_ctrl(proc), led.read_status(proc)))
"'
```

Expected: Bytecodes load and verify OK for both processors. CTRL shows LEDUP_EN=1. STATUS should show the processor is running.

**If CTRL=0x01 doesn't enable the processor** (STATUS shows no RUNNING bit), investigate:

```bash
# Scan CTRL register — write increasing values and check STATUS
ssh admin@192.168.88.12 'sudo python3 -c "
import sys, time; sys.path.insert(0, \".\")
import wedge100s_ledup as ledup
with ledup.LedupAccess() as led:
    for val in [0x01, 0x41, 0x2A01, 0x2A41, 0x42A01, 0x42A41]:
        led.write_ctrl(0, val)
        time.sleep(0.1)
        rb = led.read_ctrl(0)
        st = led.read_status(0)
        print(\"wrote 0x%08x  readback 0x%08x  status 0x%08x\" % (val, rb, st))
    # Reset
    led.write_ctrl(0, 0)
"'
```

Adjust `ctrl_candidates` in the code based on which value produces a non-zero STATUS with RUNNING indication. Update the `load_and_enable_ledup` function with the discovered working value.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py
git commit -m "feat(led-diag): add bytecode loading and LEDUP enable via /dev/mem"
```

---

## Task 7: Set Color and Set Port Commands

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py`

Software-drives LEDs by loading bytecode, writing specific DATA_RAM values, and controlling which LEDUP processors are enabled. Port mapping from the SOC file determines which DATA_RAM index corresponds to each front-panel port.

Color model: LEDUP0 drives one color channel, LEDUP1 drives the other. "both" = magenta (confirmed). "off" disables both processors for the target port(s). The `probe` command (Task 8) discovers which channel is which physical color.

- [ ] **Step 1: Implement set color and set port commands**

Add to `wedge100s-led-diag.py` before `main()`:

```python
def _ensure_bytecode_loaded(led):
    """Load bytecode if PROGRAM_RAM is empty. Returns True on success."""
    # Check if bytecode already loaded
    if led.read_program_ram(0, 0) != 0:
        return True
    soc_path = find_soc_path()
    if not soc_path:
        print("ERROR: led_proc_init.soc not found")
        return False
    return load_and_enable_ledup(led, soc_path)


def cmd_set_color(args):
    """Software-drive all 32 ports to a single color.

    Color is specified as ledup0/ledup1/both/off, referring to which
    LEDUP processor(s) output active (1) for all ports.
    """
    color = args.color
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)

    with ledup.LedupAccess() as led:
        if not _ensure_bytecode_loaded(led):
            sys.exit(1)

        soc_path = find_soc_path()
        remap = ledup.parse_soc_remap(soc_path) if soc_path else None

        for proc in (0, 1):
            if color == "off":
                # Zero all DATA_RAM entries
                led.zero_data_ram(proc)
                led.write_ctrl(proc, 0)
            else:
                # Determine if this processor should be active
                active = (color == "both" or
                          (color == "ledup0" and proc == 0) or
                          (color == "ledup1" and proc == 1))
                if active:
                    # Write link=1 to all port DATA_RAM entries
                    for i in range(ledup.NUM_PORTS):
                        led.write_data_ram(proc, i, ledup.BIT_LINK)
                else:
                    led.zero_data_ram(proc)

        # Read back and report
        for proc in (0, 1):
            ctrl = led.read_ctrl(proc)
            nz = sum(1 for i in range(ledup.NUM_PORTS) if led.read_data_ram(proc, i) != 0)
            print("LEDUP%d: CTRL=0x%08x, DATA_RAM non-zero=%d/32" % (proc, ctrl, nz))

    print("Set all ports to: %s" % color)


def cmd_set_port(args):
    """Software-drive a single front-panel port to a color.

    All other ports are set to off. Uses the SOC file remap table
    to map front-panel port number to DATA_RAM index.
    """
    fp_port = args.port_num
    color = args.color
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)

    soc_path = find_soc_path()
    if not soc_path:
        print("ERROR: led_proc_init.soc not found")
        sys.exit(1)

    remap = ledup.parse_soc_remap(soc_path)
    led_index = remap[fp_port]

    with ledup.LedupAccess() as led:
        if not _ensure_bytecode_loaded(led):
            sys.exit(1)

        for proc in (0, 1):
            # Zero all entries first
            led.zero_data_ram(proc)

            active = (color == "both" or
                      (color == "ledup0" and proc == 0) or
                      (color == "ledup1" and proc == 1))
            if active:
                led.write_data_ram(proc, led_index, ledup.BIT_LINK)

        for proc in (0, 1):
            val = led.read_data_ram(proc, led_index)
            print("LEDUP%d DATA_RAM[%d] = 0x%02x" % (proc, led_index, val))

    print("Set FP port %d (DATA_RAM[%d]) to: %s" % (fp_port, led_index, color))
```

- [ ] **Step 2: Test set color on hardware**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    admin@192.168.88.12:~

# Test: all ports with both LEDUP active (should be magenta if bytecode works)
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color both'

# Test: only LEDUP0 (should show one color channel)
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color ledup0'

# Test: only LEDUP1 (should show the other color channel)
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color ledup1'

# Test: all off
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color off'

# Test: single port (FP1)
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set port 1 both'

# Verify status
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py status'
```

Expected: Physical LED colors visible on front panel. Record which color `ledup0` and `ledup1` produce for the probe results.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py
git commit -m "feat(led-diag): add set color and set port commands"
```

---

## Task 8: Probe Command

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py`

Automated discovery of the LED color mapping. Three phases:
1. **CPLD test mode colors** — cycle `th_led_steam` and register 0x3d values
2. **BCM scan chain combinations** — test all 4 LEDUP0/LEDUP1 states
3. **Per-port walk** — light one port at a time to verify remap table

Results saved to `/run/wedge100s/led_probe_results.json`.

- [ ] **Step 1: Implement probe command**

Add to `wedge100s-led-diag.py` before `main()`:

```python
def cmd_probe(args):
    """Discover LED color mapping through automated test sequences.

    Phase 1: CPLD test mode — cycles through CPLD-generated patterns
    Phase 2: BCM scan chain — tests all 4 LEDUP0/LEDUP1 combinations
    Phase 3: Per-port walk — lights one port at a time

    Each phase pauses for manual observation. Results saved to JSON.
    """
    results = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "phases": {}}
    cpld = ledup.CpldAccess()

    # ── Phase 1: CPLD test mode colors ────────────────────────────
    print("=== Phase 1: CPLD Test Mode Colors ===")
    print("Observe the front panel LEDs and note the color/pattern.")
    phase1 = []

    # Cycle th_led_steam values (0-3) with test mode enabled
    for steam in range(4):
        val = ledup.CPLD_TEST_MODE_EN | (steam << 4)
        cpld.write_led_ctrl(val)
        readback = cpld.read_led_ctrl()
        desc = "th_led_steam=%d, 0x3c=0x%02x" % (steam, readback)
        print("\n[Phase 1.%d] %s" % (steam, desc))
        print("  Press Enter after observing (type color description): ", end="")
        observation = input().strip() or "not recorded"
        phase1.append({"steam": steam, "reg_0x3c": "0x%02x" % readback,
                       "observation": observation})

    # Also test blink + walk
    for name, val in [("blink", 0xE0), ("walk", 0x08)]:
        cpld.write_led_ctrl(val)
        readback = cpld.read_led_ctrl()
        print("\n[Phase 1.%s] 0x3c=0x%02x" % (name, readback))
        print("  Press Enter after observing (type description): ", end="")
        observation = input().strip() or "not recorded"
        phase1.append({"mode": name, "reg_0x3c": "0x%02x" % readback,
                       "observation": observation})

    results["phases"]["cpld_test_modes"] = phase1

    # ── Phase 2: BCM scan chain combinations ──────────────────────
    print("\n=== Phase 2: BCM Scan Chain Combinations ===")
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
    phase2 = []

    soc_path = find_soc_path()
    with ledup.LedupAccess() as led:
        # Load bytecode first
        if soc_path:
            _ensure_bytecode_loaded(led)

        combos = [
            ("off/off", False, False),
            ("on/off", True, False),
            ("off/on", False, True),
            ("on/on", True, True),
        ]

        for label, ledup0_on, ledup1_on in combos:
            for proc in (0, 1):
                active = (proc == 0 and ledup0_on) or (proc == 1 and ledup1_on)
                if active:
                    for i in range(ledup.NUM_PORTS):
                        led.write_data_ram(proc, i, ledup.BIT_LINK)
                else:
                    led.zero_data_ram(proc)

            print("\n[Phase 2] LEDUP0=%s LEDUP1=%s" % (
                "active" if ledup0_on else "off",
                "active" if ledup1_on else "off"))
            print("  Press Enter after observing (type color): ", end="")
            observation = input().strip() or "not recorded"
            phase2.append({
                "ledup0": "active" if ledup0_on else "off",
                "ledup1": "active" if ledup1_on else "off",
                "observation": observation,
            })

    results["phases"]["bcm_scan_chain"] = phase2

    # ── Phase 3: Per-port walk ────────────────────────────────────
    print("\n=== Phase 3: Per-Port Walk ===")
    print("Lighting one port at a time (both LEDUP channels).")
    phase3 = []

    if soc_path:
        remap = ledup.parse_soc_remap(soc_path)
    else:
        print("WARNING: no SOC file, using identity mapping")
        remap = {fp: fp - 1 for fp in range(1, 33)}

    with ledup.LedupAccess() as led:
        _ensure_bytecode_loaded(led)

        for fp in range(1, ledup.NUM_PORTS + 1):
            led_idx = remap[fp]
            for proc in (0, 1):
                led.zero_data_ram(proc)
                led.write_data_ram(proc, led_idx, ledup.BIT_LINK)

            print("[Phase 3] FP%d (DATA_RAM[%d]) — observe which cage lights up: " % (
                fp, led_idx), end="")
            observation = input().strip() or "ok"
            phase3.append({"fp_port": fp, "data_ram_index": led_idx,
                           "observation": observation})

        # Clean up
        for proc in (0, 1):
            led.zero_data_ram(proc)

    results["phases"]["per_port_walk"] = phase3

    # ── Save results ──────────────────────────────────────────────
    os.makedirs(ledup.RUN_DIR, exist_ok=True)
    with open(PROBE_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to %s" % PROBE_RESULTS_PATH)

    # Restore passthrough
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
    print("Restored CPLD to passthrough mode.")
```

- [ ] **Step 2: Test probe on hardware (interactive)**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    admin@192.168.88.12:~

# Run probe — this is interactive, requires someone at the switch
ssh -t admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py probe'
```

Expected: Cycles through CPLD test modes, BCM scan chain combinations, and per-port walk. Observer records colors at each step. Results saved to JSON.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py
git commit -m "feat(led-diag): add probe command for color mapping discovery"
```

---

## Task 9: Set Passthrough Command

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py`

Restores the full LED pipeline: CPLD in passthrough mode (0x02), LED bytecode loaded, LEDUP processors enabled with hardware auto-update. This is the "fix" command that replaces the broken ledinit.

**Note on PORT_ORDER_REMAP:** The remap registers have unknown BAR2 offsets. This task uses the bytecode-only approach: load PROGRAM_RAM and enable LEDUP. The hardware auto-update populates DATA_RAM in the default port order. If the physical LED positions are wrong after this, the remap register offsets need to be discovered (requires bcmcmd access or SDK headers from the build container). Add a follow-up task to discover and write REMAP registers if needed.

- [ ] **Step 1: Implement set passthrough command**

Add to `wedge100s-led-diag.py` before `main()`:

```python
def cmd_set_passthrough(args):
    """Restore full LED pipeline: CPLD passthrough + bytecode + auto.

    Equivalent to what ledinit should do via bcmcmd:
    1. Load led_proc_init.soc bytecode into PROGRAM_RAM
    2. Enable LEDUP processors
    3. Set CPLD to passthrough mode (0x02)

    PORT_ORDER_REMAP registers are NOT written (BAR2 offsets unknown).
    If port-to-LED mapping is wrong, remap offsets must be discovered
    separately via bcmcmd or SDK headers.
    """
    soc_path = find_soc_path()
    if not soc_path:
        print("ERROR: led_proc_init.soc not found")
        sys.exit(1)

    # Set CPLD to passthrough
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
    readback = cpld.read_led_ctrl()
    print("CPLD 0x3c = 0x%02x — %s" % (
        readback, "PASS" if readback == ledup.CPLD_PASSTHROUGH else "FAIL"))

    # Load bytecode and enable LEDUP
    with ledup.LedupAccess() as led:
        ok = load_and_enable_ledup(led, soc_path)
        if ok:
            print("\nPassthrough mode active. LEDUP processors running.")
            print("Port LEDs should now reflect live link/speed status.")
            print("\nNOTE: PORT_ORDER_REMAP not configured via /dev/mem.")
            print("If port LEDs show wrong positions, remap offsets need discovery.")
        else:
            print("\nWARNING: Bytecode loaded but LEDUP enable may have failed.")
            print("Run 'status' to inspect register state.")

    # Final status
    print("\n--- Post-passthrough status ---")
    cmd_status(argparse.Namespace())
```

- [ ] **Step 2: Test set passthrough on hardware**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    admin@192.168.88.12:~

# First, ensure we're starting from a clean state
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set all-off'

# Load passthrough
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set passthrough'
```

Expected: CPLD shows 0x02, both LEDUP processors enabled with loaded bytecode. If link-up ports exist, their LEDs should light up. Note any wrong port-to-position mapping.

- [ ] **Step 3: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py
git commit -m "feat(led-diag): add set passthrough command (bytecode loading via /dev/mem)"
```

---

## Task 10: BMC Daemon — Add CPLD LED Read Dispatch

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c`

Add a `cpld_led_ctrl.set` entry to the daemon's `write_requests[]` dispatch table. When the host writes this `.set` file, the daemon reads CPLD register 0x3c via SSH and writes the result to `/run/wedge100s/cpld_led_ctrl`. This allows the CPLD access layer to read registers without direct SSH from the diag tool when the daemon is running.

- [ ] **Step 1: Add dispatch entry to wedge100s-bmc-daemon.c**

In `wedge100s-bmc-daemon.c`, find the `write_requests[]` array (line 231) and add the new entry:

```c
static const struct {
    const char *setfile;
    const char *bmc_cmd;
} write_requests[] = {
    { "clear_led_diag.set", "/usr/local/bin/clear_led_diag.sh" },
    { "cpld_led_ctrl.set",  "i2cget -f -y 12 0x31 0x3c" },
};
```

Then modify `dispatch_write_requests()` to handle the `cpld_led_ctrl.set` case specially — it needs to capture output and write it to a file, not just execute and discard:

Actually, the current dispatch mechanism just runs the command and discards output (`bmc_run` redirects to `/dev/null`). For a read-back we need `bmc_read_int`. This is a more involved change. Let's add a separate handler.

Replace the dispatch loop body in `dispatch_write_requests()` (lines 255-267):

```c
static void dispatch_write_requests(int inotify_fd)
{
    char ibuf[sizeof(struct inotify_event) + NAME_MAX + 1];
    ssize_t n;

    while ((n = read(inotify_fd, ibuf, sizeof(ibuf))) > 0) {
        struct inotify_event *ev = (struct inotify_event *)ibuf;
        char path[256];
        size_t i, nlen;

        if (!(ev->mask & IN_CLOSE_WRITE) || ev->len == 0)
            continue;

        nlen = strlen(ev->name);
        if (nlen < 4 || strcmp(ev->name + nlen - 4, ".set") != 0)
            continue;

        snprintf(path, sizeof(path), RUN_DIR "/%s", ev->name);
        unlink(path);

        /* Special case: cpld_led_ctrl.set → read register, write result file */
        if (strcmp(ev->name, "cpld_led_ctrl.set") == 0) {
            int val;
            syslog(LOG_INFO, "wedge100s-bmc-daemon: reading CPLD 0x3c");
            if (bmc_ensure_connected() == 0 &&
                bmc_read_int("i2cget -f -y 12 0x31 0x3c", 0, &val) == 0) {
                snprintf(path, sizeof(path), RUN_DIR "/cpld_led_ctrl");
                write_file(path, val);
            }
            continue;
        }

        for (i = 0; i < sizeof(write_requests) / sizeof(write_requests[0]); i++) {
            if (strcmp(ev->name, write_requests[i].setfile) == 0) {
                syslog(LOG_INFO, "wedge100s-bmc-daemon: dispatching %s", ev->name);
                if (bmc_ensure_connected() == 0)
                    bmc_run(write_requests[i].bmc_cmd);
                break;
            }
        }
    }
}
```

- [ ] **Step 2: Build the daemon**

```bash
# Build on the target (or cross-compile in build container)
ssh admin@192.168.88.12 'cd ~ && gcc -O2 -o wedge100s-bmc-daemon wedge100s-bmc-daemon.c'
```

Or build locally if cross-compilation is set up:
```bash
gcc -O2 -o /tmp/wedge100s-bmc-daemon platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c
scp /tmp/wedge100s-bmc-daemon admin@192.168.88.12:~
```

- [ ] **Step 3: Test the new dispatch entry**

```bash
# Stop daemon, replace binary, restart
ssh admin@192.168.88.12 'sudo systemctl stop wedge100s-bmc-daemon && \
    sudo cp wedge100s-bmc-daemon /usr/bin/ && \
    sudo systemctl start wedge100s-bmc-daemon'

# Trigger the read
ssh admin@192.168.88.12 'sudo touch /run/wedge100s/cpld_led_ctrl.set && \
    sleep 2 && \
    cat /run/wedge100s/cpld_led_ctrl'
```

Expected: Output should be `2` (decimal value of 0x02 = passthrough mode).

```bash
# Check syslog for dispatch message
ssh admin@192.168.88.12 'journalctl -u wedge100s-bmc-daemon --since "1 minute ago" | grep cpld'
```

Expected: `wedge100s-bmc-daemon: reading CPLD 0x3c`

- [ ] **Step 4: Commit**

```bash
git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c
git commit -m "feat(bmc-daemon): add cpld_led_ctrl.set dispatch for CPLD 0x3c reads"
```

---

## Task 11: dsserve/bcmcmd Investigation

**Files:**
- Read-only investigation (no code changes expected unless fix is found)
- Write findings to: `notes/2026-04-02-dsserve-investigation.md`

Investigate why bcmcmd cannot connect to the dsserve socket inside the syncd container. This blocks the normal `ledinit` from loading LED bytecode. The `/dev/mem` approach from Tasks 6-9 is a working bypass, but fixing dsserve would restore the standard pipeline.

- [ ] **Step 1: Examine ledinit inside syncd container**

```bash
# Find ledinit script/binary in the syncd container
ssh admin@192.168.88.12 'docker exec syncd find / -name "ledinit" -o -name "led_init" 2>/dev/null'

# Check what ledinit does
ssh admin@192.168.88.12 'docker exec syncd cat /usr/bin/ledinit 2>/dev/null || \
    docker exec syncd file /usr/bin/ledinit 2>/dev/null'

# Check if it uses bcmcmd
ssh admin@192.168.88.12 'docker exec syncd grep -l bcmcmd /usr/bin/ledinit 2>/dev/null || \
    docker exec syncd strings /usr/bin/ledinit 2>/dev/null | grep -i "bcm\|soc\|led"'
```

- [ ] **Step 2: Check syncd diag shell configuration**

```bash
# Look for diag shell flags in SAI profile
ssh admin@192.168.88.12 'docker exec syncd cat /etc/sai.d/sai.profile'

# Check syncd command line
ssh admin@192.168.88.12 'docker exec syncd cat /proc/1/cmdline | tr "\0" " "'

# Check dsserve socket state
ssh admin@192.168.88.12 'docker exec syncd ls -la /var/run/sswsyncd/'
ssh admin@192.168.88.12 'docker exec syncd ss -lxp | grep sswsyncd'

# Try bcmcmd from inside the container
ssh admin@192.168.88.12 'docker exec syncd timeout 5 bcmcmd "echo hello" 2>&1 || echo "bcmcmd failed"'
```

- [ ] **Step 3: Check for known SONiC issues with dsserve**

```bash
# Check syncd thread list for diag-related threads
ssh admin@192.168.88.12 'docker exec syncd ps -eLf | head -50'

# Check if diag shell is compiled in
ssh admin@192.168.88.12 'docker exec syncd ldd /usr/bin/syncd 2>/dev/null | grep -i diag'
ssh admin@192.168.88.12 'docker exec syncd strings /usr/bin/syncd | grep -i "diag_shell\|DIAG"'
```

- [ ] **Step 4: Document findings**

Write investigation results to `notes/2026-04-02-dsserve-investigation.md` including:
- What ledinit does and why it fails silently
- Whether the diag shell is compiled into syncd
- Whether bcmcmd works from inside vs outside the container
- Recommended fix (if found) or confirmation that /dev/mem bypass is the right approach

```bash
git add notes/2026-04-02-dsserve-investigation.md
git commit -m "docs: add dsserve/bcmcmd investigation findings"
```

---

## Task 12: Integration Test and Cleanup

**Files:**
- All files from previous tasks (final verification)
- Create: `notes/2026-04-02-led-diag-tooling-results.md` (hardware test results)

End-to-end verification on hardware: run through all commands, confirm LED behavior, document results.

- [ ] **Step 1: Deploy final versions of all files**

```bash
scp platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s_ledup.py \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-led-diag.py \
    admin@192.168.88.12:~
```

- [ ] **Step 2: Run full command cycle**

```bash
# Status baseline
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py status'

# Rainbow
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set rainbow'
# (observe rainbow)

# All off
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set all-off'
# (observe all dark)

# Color: each channel
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color ledup0'
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color ledup1'
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set color both'

# Single port
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set port 1 both'
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set port 16 ledup0'

# Passthrough (restore normal operation)
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py set passthrough'

# Final status
ssh admin@192.168.88.12 'sudo python3 wedge100s-led-diag.py status'
```

- [ ] **Step 3: Run unit tests on dev host**

```bash
cd /export/sonic/sonic-buildimage.claude && python3 -m pytest tests/test_wedge100s_ledup.py -v
```

Expected: All unit tests pass.

- [ ] **Step 4: Document results**

Write hardware test results to `notes/2026-04-02-led-diag-tooling-results.md`:
- Which CTRL value worked for LEDUP enable
- Observed colors for each LEDUP channel
- Whether port-to-position mapping was correct
- Any issues discovered

- [ ] **Step 5: Final commit**

```bash
git add notes/2026-04-02-led-diag-tooling-results.md
git commit -m "docs: add LED diagnostic tooling hardware test results"
```

---

## Dependency Graph

```
Task 1 (constants + SOC parser)
  ├→ Task 2 (BAR2 access class)
  │    ├→ Task 4 (status command)
  │    ├→ Task 5 (rainbow + all-off)
  │    └→ Task 6 (bytecode loading)
  │         ├→ Task 7 (set color + set port)
  │         ├→ Task 8 (probe)
  │         └→ Task 9 (set passthrough)
  └→ Task 3 (CPLD access)
       ├→ Task 4 (status command)
       ├→ Task 5 (rainbow + all-off)
       └→ Task 10 (BMC daemon dispatch)

Task 11 (dsserve investigation) — independent, can run anytime
Task 12 (integration test) — after all others
```

Tasks 1, 2, 3 can be done in sequence (they build on each other in the same file).
Tasks 4+5 can be done together (both need library complete).
Tasks 7, 8, 9 depend on Task 6.
Tasks 10 and 11 are independent of the main chain.

## Known Risks

1. **CTRL register bit layout**: Unknown exact bit positions for SCAN_START_DELAY and INTRA_PORT_DELAY. Mitigated by trying multiple candidate values in Task 6. If none work, the CTRL register format must be discovered from SDK headers inside the build container.

2. **PORT_ORDER_REMAP BAR2 offsets**: Unknown. `set passthrough` loads bytecode but cannot configure the hardware remap. If port-to-LED mapping is wrong in passthrough mode, these offsets must be discovered. Possible approaches: (a) fix bcmcmd via Task 11, (b) find offsets in BCM SDK headers inside `sonic-slave-trixie` build container, (c) empirical scan of 0x34000-0x340FF register space.

3. **`led auto` control register**: The mechanism to enable/disable hardware auto-population of DATA_RAM is unknown. If hardware overwrites software-written DATA_RAM values, the `set color`/`set port` commands will appear to work but then revert. Mitigated by disabling LEDUP_EN when doing software writes (which stops the processor entirely).

4. **Bytecode behavior assumptions**: We assume the bytecode outputs scan chain "1" for any port with DATA_RAM bit 7 (Link) = 1. This was observed empirically but not confirmed from bytecode disassembly. If `set color` produces no visible LED output, the bytecode may require additional status bits (speed, duplex).
