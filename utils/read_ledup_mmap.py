#!/usr/bin/env python3
"""Read BCM56960 LEDUP DATA_RAM via PCIe BAR2 memory-mapped register access.

Bypasses bcmcmd/dsserve — reads CMIC registers directly from /dev/mem.
Must run as root.

CMIC LEDUP register offsets within BAR2 (Tomahawk BCM56960):
  CMIC_LEDUP0_DATA_RAM: 0x34800 + 4*index  (index 0..255)
  CMIC_LEDUP1_DATA_RAM: 0x34c00 + 4*index
  CMIC_LEDUP2_DATA_RAM: 0x35000 + 4*index

Bit layout per DATA_RAM entry (lower 8 bits):
  7: Link Up    6: Flow Control   5: Full Duplex
  4:3: Speed (00=10M, 01=100M, 10=1G, 11=10G+)
  2: Collision  1: TX activity    0: RX activity
"""

import mmap
import os
import struct
import sys

# BCM56960 PCI BDF and BAR2 (8MB CMIC register space)
BCM_VID = "0x14e4"
BCM_DID = "0xb960"

LEDUP_BASES = {0: 0x34800, 1: 0x34C00, 2: 0x35000}
SPEED_NAMES = ["10M", "100M", "1G", "10G+"]
ENTRIES = 32


def find_bcm_bar2():
    for dev in os.listdir("/sys/bus/pci/devices"):
        devpath = "/sys/bus/pci/devices/" + dev
        try:
            vid = open(devpath + "/vendor").read().strip()
            did = open(devpath + "/device").read().strip()
        except OSError:
            continue
        if vid == BCM_VID and did == BCM_DID:
            lines = open(devpath + "/resource").read().strip().split("\n")
            # BAR2 is line index 2
            parts = lines[2].split()
            start = int(parts[0], 16)
            end = int(parts[1], 16)
            if start == 0:
                continue  # no BAR2, try next function
            return start, end - start + 1
    return None, None


def decode(val):
    if val == 0:
        return "(dark)"
    parts = []
    if val & 0x80:
        parts.append("Link")
    if val & 0x40:
        parts.append("FC")
    if val & 0x20:
        parts.append("FD")
    parts.append(SPEED_NAMES[(val >> 3) & 3])
    if val & 0x04:
        parts.append("Col")
    if val & 0x02:
        parts.append("TX")
    if val & 0x01:
        parts.append("RX")
    return " ".join(parts)


def main():
    procs = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else [0, 1]

    bar_addr, bar_size = find_bcm_bar2()
    if bar_addr is None:
        print("ERROR: BCM56960 BAR2 not found")
        sys.exit(1)

    print("BCM56960 BAR2: 0x%x  size=0x%x" % (bar_addr, bar_size))

    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    mm = mmap.mmap(fd, bar_size, mmap.MAP_SHARED, mmap.PROT_READ,
                   offset=bar_addr)

    for proc in procs:
        base = LEDUP_BASES[proc]
        print("\n=== LEDUP%d DATA_RAM ===" % proc)
        print("%-8s %-8s %s" % ("Entry", "Hex", "Flags"))
        for i in range(ENTRIES):
            mm.seek(base + 4 * i)
            raw = struct.unpack("<I", mm.read(4))[0]
            val = raw & 0xFF
            print("[%-3d]    0x%02x     %s" % (i, val, decode(val)))
        print()

    mm.close()
    os.close(fd)


if __name__ == "__main__":
    main()
