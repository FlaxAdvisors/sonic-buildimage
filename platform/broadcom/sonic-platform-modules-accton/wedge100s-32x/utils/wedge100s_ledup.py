#!/usr/bin/env python3
"""wedge100s_ledup.py -- BCM56960 LEDUP register access and SOC file parsing.

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

# -- BCM56960 LEDUP register offsets within PCIe BAR2 ---------------------
# These are CMIC space offsets (0x2xxxx), NOT iProc space (0x3xxxx).
# Confirmed by scanning BAR2 for bytecode signature 02 FD 42 80 at 0x20800.
# (verified on hardware 2026-04-03)

LEDUP0_CTRL = 0x20000
LEDUP0_STATUS = 0x20004
LEDUP0_PROGRAM_RAM_BASE = 0x20800   # + 4*n, n=0..255
LEDUP0_DATA_RAM_BASE = 0x20400      # + 4*n, n=0..255
LEDUP1_CTRL = 0x21000
LEDUP1_STATUS = 0x21004
LEDUP1_PROGRAM_RAM_BASE = 0x21800
LEDUP1_DATA_RAM_BASE = 0x21400

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


# -- SOC file parsing -----------------------------------------------------

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
    Uses LEDUP0 remap only (LEDUP0 and LEDUP1 are identical for
    positions 0-31).
    """
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
