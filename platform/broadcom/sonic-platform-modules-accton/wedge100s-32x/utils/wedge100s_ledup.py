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
