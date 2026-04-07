# Detecting DAC Cable Breakout Configuration from EEPROM

**Date:** 2026-04-06  
**Platform:** Accton Wedge 100S-32X (Broadcom Tomahawk BCM56960)  
**Method:** Reading `/run/wedge100s/sfp_N_eeprom` cache files via the safe sysfs interface

---

## Background

The Wedge 100S has 32 QSFP28 ports, each running 4×25G serdes lanes for 100G aggregate.
DAC (Direct Attach Copper) cables come in several topologies:

- **Straight**: QSFP28 ↔ QSFP28, all 4 lanes end-to-end — no breakout needed
- **2×SFP28 fanout**: QSFP28 ↔ 2×SFP28 — configure as **2×50G** (2 lanes per sub-port)
- **4×SFP28 fanout**: QSFP28 ↔ 4×SFP28 — configure as **4×25G** (1 lane per sub-port)
- **4×SFP+ fanout**: QSFP+ ↔ 4×SFP+ (40G → 4×10G) — configure as **4×10G**

The EEPROM alone cannot reliably distinguish straight from fanout cables via compliance
bytes — both report `ext_comp=0x0b` (100G passive copper). The vendor part number is
the definitive indicator.

---

## EEPROM Classification Method

All 256-byte EEPROM cache files follow the SFF-8636 layout:
- **Bytes 0–127**: Lower memory page (dynamic monitoring data, status flags)
- **Bytes 128–255**: Upper memory page 00 (static module identity)

Key bytes for cable classification:

| Byte (file offset) | Field | DAC value | Optical value |
|--------------------|-------|-----------|---------------|
| 0 | Identifier | 0x11 (QSFP28) or 0x0d (QSFP+) | same |
| 130 | Connector type | 0x23 (no separable connector) | 0x00/0x02/0x07/0x0c |
| 146 | Copper cable length (metres) | 1–5 | 0 |
| 147 | Device technology (bits 7:4) | 0xa_ (copper unequalized) | 0x0_ / 0x4_ / 0x6_ |
| 192 | Extended compliance code | 0x0b passive / 0x0c active | 0x02/0x03/0x06/0x09 |
| 148–163 | Vendor name (16 bytes ASCII) | e.g. "Mellanox", "FS", "Amphenol" | |
| 168–183 | Vendor part number (16 bytes ASCII) | e.g. "MCP7H00-G003" | |

**Device technology nibble (byte 147 >> 4):**

| Nibble | Meaning | Category |
|--------|---------|----------|
| 0x0 | 850 nm VCSEL | Optical (SR4) |
| 0x4 | 1310 nm DFB | Optical (LR4 / CWDM4) |
| 0x6 | 1310 nm EML | Optical (LR4) |
| 0xa | Copper cable unequalized | **DAC** |
| 0xb | Copper cable passive equalized | **DAC** |

**Extended compliance (byte 192):**

| Value | Meaning |
|-------|---------|
| 0x02 | 100GBASE-SR4 or 25GBASE-SR |
| 0x03 | 100GBASE-LR4 or 25GBASE-LR |
| 0x06 | 100GBASE-SM SR (CWDM4 / CLR4) |
| 0x0b | **100G passive copper (DAC)** |
| 0x0c | 100G active copper (DAC) |

---

## Cable Models Found in This Testbed

### Mellanox MCP1600-C01A — QSFP28 ↔ QSFP28, 2 m straight

- **Fanout:** None (1×100G ↔ 1×100G)
- **Topology:** All 4 serdes lanes run end-to-end between two QSFP28 cages
- **SONiC config:** `1x100G` (default, no DPB needed)
- **EEPROM signature:** identifier=0x11, connector=0x23, cu_len=2m, dev_tech=0xa0, ext_comp=0x0b
- **Vendor name:** Mellanox; **PN:** MCP1600-C01A

### FS Q28-PC01 / Q28-PC02 / Q28-PC03 / Q28-PC05 — QSFP28 ↔ QSFP28, 1/2/3/5 m straight

- **Fanout:** None (1×100G ↔ 1×100G)
- **Topology:** Generic QSFP28 passive copper; suffix encodes cable length in metres
- **SONiC config:** `1x100G` (default, no DPB needed)
- **EEPROM signature:** identifier=0x11, connector=0x23, cu_len=1–5m, dev_tech=0xa0, ext_comp=0x0b
- **Vendor name:** FS; **PN:** Q28-PC01 / Q28-PC02 / Q28-PC03 / Q28-PC05

### Amphenol NDAQGF-F302 / NDAQGF-F305 — QSFP28 ↔ 4×SFP28

- **Fanout:** 1×100G → **4×25G** (one SFP28 connector per serdes lane)
- **Topology:** QSFP28 cage on switch side; four SFP28 tails on host/server side
- **SONiC config:** `4x25G[10G]` — four sub-ports at 25G each
- **EEPROM signature:** identifier=0x11, connector=0x23, dev_tech=0xa0, ext_comp=0x0b
  - F302: cu_len=2m; F305: cu_len=1m (EEPROM-reported length)
- **Vendor name:** Amphenol; **PN:** NDAQGF-F302 / NDAQGF-F305
- **Reference:** Amphenol SF-NDAQGF100G series datasheet (100G QSFP28 to 4×25G SFP28 DAC splitter)

### Mellanox MCP7H00-G003 — QSFP28 ↔ 2×SFP28, 3 m

- **Fanout:** 1×100G → **2×50G** (two SFP28 connectors, 2 serdes lanes each)
- **Topology:** QSFP28 cage on switch side; two SFP28 tails on host/server side
- **SONiC config:** `2x50G` — two sub-ports at 50G each (Ethernet20 + Ethernet22)
- **EEPROM signature:** identifier=0x11, connector=0x23, cu_len=3m, dev_tech=0xa0, ext_comp=0x0b
  - Distinguishing byte: 0x04 at upper-page offset 134 (SAS compliance field, vendor use)
- **Vendor name:** Mellanox; **PN:** MCP7H00-G003

### Mellanox MCP7904-X002A — QSFP+ ↔ 4×SFP+, 2 m active

- **Fanout:** 1×40G → **4×10G** (four SFP+ connectors, 1 serdes lane each)
- **Topology:** QSFP+ cage on switch side (identifier=**0x0d**, not 0x11); four SFP+ tails
- **SONiC config:** `4x25G[10G]` mode applied as 10G — four sub-ports at 10G each
- **EEPROM signature:** identifier=**0x0d** (QSFP+), connector=0x23, cu_len=2m, dev_tech=0xa0, ext_comp=**0x00**
- **Vendor name:** Mellanox; **PN:** MCP7904-X002A
- **Note:** The 0x0d identifier is the reliable distinguisher from QSFP28 fanout cables

---

## Optical Transceivers Found (no action taken)

| Port | ext_comp | dev_tech | PN (upper page) | Type |
|------|----------|----------|-----------------|------|
| 19 | 0x06 | 0x40 | AQPLBCQ4EDMA1105 | 100GBASE-CWDM4 / SM-SR |
| 21 | 0x02 | 0x00 | QSFP28-SR4-100G | 100GBASE-SR4 |
| 25 | 0x02 | 0x00 | QSFP28-SR4-100G | 100GBASE-SR4 |
| 26 | 0x03 | 0x64 | QSFP28-LR4-100G | 100GBASE-LR4 |
| 27 | 0x02 | 0x00 | QSFP28-SR4-100G | 100GBASE-SR4 |
| 29 | 0x06 | 0x40 | C100QSFPCWDM400B | 100GBASE-CWDM4 |

---

## DAC Port Inventory: Before and After

QSFP physical slot → Ethernet mapping derived from `index` field in `CONFIG_DB PORT` table
(index = QSFP slot + 1).

| QSFP | Ethernet (before) | Speed (before) | Cable | Fanout | Ethernet (after) | Speed (after) | Change |
|------|-------------------|---------------|-------|--------|------------------|--------------|--------|
| 0  | Ethernet0–3   | 4 × 25G  | Amphenol NDAQGF-F302 | 4×SFP28  | Ethernet0–3   | 4 × 25G  | none — already correct |
| 2  | Ethernet8     | 1 × 100G | Mellanox MCP1600-C01A | straight | Ethernet8     | 1 × 100G | none — straight cable |
| 3  | Ethernet12    | 1 × 100G | FS Q28-PC03           | straight | Ethernet12    | 1 × 100G | none — straight cable |
| 4  | Ethernet16    | 1 × 100G | FS Q28-PC02           | straight | Ethernet16    | 1 × 100G | none — straight cable |
| 5  | Ethernet20    | 1 × 100G | Mellanox MCP7H00-G003 | 2×SFP28  | Ethernet20,22 | 2 × 50G  | **DPB applied** |
| 6  | Ethernet24    | 1 × 100G | FS Q28-PC05           | straight | Ethernet24    | 1 × 100G | none — straight cable |
| 7  | Ethernet28    | 1 × 100G | FS Q28-PC05           | straight | Ethernet28    | 1 × 100G | none — straight cable |
| 8  | Ethernet32    | 1 × 100G | FS Q28-PC02           | straight | Ethernet32    | 1 × 100G | none — straight cable |
| 12 | Ethernet48    | 1 × 100G | FS Q28-PC02           | straight | Ethernet48    | 1 × 100G | none — straight cable |
| 16 | Ethernet64–67 | 4 × 10G  | Mellanox MCP7904-X002A | 4×SFP+  | Ethernet64–67 | 4 × 10G  | none — already correct |
| 20 | Ethernet80–83 | 4 × 25G  | Amphenol NDAQGF-F305  | 4×SFP28  | Ethernet80–83 | 4 × 25G  | none — already correct |
| 28 | Ethernet112   | 1 × 100G | FS Q28-PC01           | straight | Ethernet112   | 1 × 100G | none — straight cable |

### DPB applied: Ethernet20 → 2×50G

```
sudo config interface breakout Ethernet20 2x50G
sudo config save -y
```

Result (verified in `CONFIG_DB`):

| Sub-port | Lanes | Speed | Alias |
|----------|-------|-------|-------|
| Ethernet20 | 1, 2 | 50G | Ethernet6/1 |
| Ethernet22 | 3, 4 | 50G | Ethernet6/3 |

---

## Limitations of EEPROM-Only Detection

1. **Straight vs. fanout is not encoded** in any standard compliance byte. `ext_comp=0x0b`
   applies equally to straight and fanout passive copper. Part number lookup is required.

2. **Vendor name and PN fields** (upper-page bytes 148–183) are reliable only if the module
   populates them; some white-label cables leave these blank.

3. **MCP7H00 vs. MCP1600 are indistinguishable by compliance bytes alone.** The only
   EEPROM difference observed was a non-standard byte at upper-page offset 134 (=0x04 on
   MCP7H00, =0x00 on MCP1600). This field is defined as SAS/SATA compliance and is not a
   documented fanout indicator — treat as coincidental.

4. **QSFP+ (0x0d) vs. QSFP28 (0x11)** is a reliable identifier for 40G vs. 100G modules
   and distinguishes MCP7904 (4×10G) from QSFP28 fanout cables.

---

## Test Node Neighbor Topology (LLDP + lsnet, 2026-04-07)

Test nodes have one or two Mellanox PCIe NICs each, connected to hare-lorax (192.168.88.12)
and rabbit-lorax (192.168.88.14) via DACs. Management IPs are on the rabbit-lorax-facing
port (ens1f0np0); test-plane IPs (10.0.10.0/24) are on the hare-lorax-facing port.

Node naming convention: `test-et<QSFP>b<lane>` — named after the hare-lorax breakout
sub-port the CX4 ens1f1np1 connects to.

### test-et6b1 — 192.168.88.236

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX6 Dx (0x1017) | ens3f0np0 | 08:c0:eb:56:6b:ce | 16.30.1004 | 01:00.0 | hare-lorax | Ethernet6/1 (Ethernet20, 50G) | — | up |
| CX6 Dx (0x1017) | ens3f1np1 | 08:c0:eb:56:6b:cf | 16.30.1004 | 01:00.1 | hare-lorax | Ethernet8/1 (Ethernet28, 100G) | — | up |
| CX4 (0x1015) | ens1f0np0 | 1c:34:da:7f:b3:a2 | 14.26.4012 | 03:00.0 | rabbit-lorax | Ethernet6/1 | 192.168.88.236/24 | up |
| CX4 (0x1015) | ens1f1np1 | 1c:34:da:7f:b3:a3 | 14.26.4012 | 03:00.1 | — | — | — | **down** |

### test-et6b3 — 192.168.88.243 (BMC: 192.168.88.244)

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX6 Dx (0x1017) | ens3f0np0 | b8:ce:f6:0a:f7:c4 | 16.28.1002 | 01:00.0 | hare-lorax | Ethernet6/3 (Ethernet22, 50G) | — | up |
| CX6 Dx (0x1017) | ens3f1np1 | b8:ce:f6:0a:f7:c5 | 16.28.1002 | 01:00.1 | hare-lorax | Ethernet7/1 (Ethernet24, 100G) | — | up |
| CX4 (0x1015) | ens1f0np0 | 0c:42:a1:19:93:1a | 14.26.4012 | 03:00.0 | rabbit-lorax | Ethernet6/3 | 192.168.88.243/24 | up |
| CX4 (0x1015) | ens1f1np1 | 0c:42:a1:19:93:1b | 14.26.4012 | 03:00.1 | hare-lorax | Ethernet1/1 (Ethernet0, 25G) | 10.0.10.243/24 | up |

### test-et8b4 — 192.168.88.232 (BMC: 192.168.88.218)

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX4 (0x1015) | ens1f0np0 | b8:ce:f6:fe:8a:ec | 14.26.4012 | 08:00.0 | rabbit-lorax | Ethernet8/4 | 192.168.88.232/24 | up |
| CX4 (0x1015) | ens1f1np1 | b8:ce:f6:fe:8a:ed | 14.26.4012 | 08:00.1 | hare-lorax | Ethernet1/2 (Ethernet1, 25G) | 10.0.10.232/24 | up |

### test-et7b3 — 192.168.88.237 (BMC: 192.168.88.224)

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX4 (0x1015) | ens1f0np0 | 0c:42:a1:19:99:da | 14.26.4012 | 02:00.0 | rabbit-lorax | Ethernet7/3 | 192.168.88.237/24 | up |
| CX4 (0x1015) | ens1f1np1 | 0c:42:a1:19:99:db | 14.26.4012 | 02:00.1 | hare-lorax | Ethernet17/3 (Ethernet66, 25G) | 10.0.10.237/24 | up |

### test-et25b1 — 192.168.88.242 (BMC: 192.168.88.229)

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX6 Dx (0x1017) | ens3f0np0 | 08:c0:eb:56:6b:fe | 16.30.1004 | 01:00.0 | — | — | — | **down** |
| CX6 Dx (0x1017) | ens3f1np1 | 08:c0:eb:56:6b:ff | 16.30.1004 | 01:00.1 | — | — | — | **down** |
| CX4 (0x1015) | ens1f0np0 | 0c:42:a1:19:9b:5a | 14.26.4012 | 03:00.0 | rabbit-lorax | Ethernet25/1 | (via br0) 192.168.88.242/24 | up |
| CX4 (0x1015) | ens1f1np1 | 0c:42:a1:19:9b:5b | 14.26.4012 | 03:00.1 | hare-lorax | Ethernet17/4 (Ethernet67, 25G) | 10.0.10.242/24 | up |

### test-et6b2 — 192.168.88.227

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX4 (0x1015) | ens1f0np0 | 08:c0:eb:d1:8d:c6 | 14.26.4012 | 02:00.0 | rabbit-lorax | Ethernet6/2 | 192.168.88.227/24 | up |
| CX4 (0x1015) | ens1f1np1 | 08:c0:eb:d1:8d:c7 | 14.26.4012 | 02:00.1 | hare-lorax | Ethernet21/2 (Ethernet81, 25G) | 10.0.10.241/24 | up |

### test-et7b1 — 192.168.88.225 (BMC: 192.168.88.216)

| NIC | Interface | MAC | FW | PCI | Connects To | Port | IP | Link |
|-----|-----------|-----|----|-----|-------------|------|----|------|
| CX4 (0x1015) | ens1f0np0 | 1c:34:da:7f:9d:32 | 14.26.4012 | 02:00.0 | rabbit-lorax | Ethernet7/1 | 192.168.88.225/24 | up |
| CX4 (0x1015) | ens1f1np1 | 1c:34:da:7f:9d:33 | 14.26.4012 | 02:00.1 | hare-lorax | Ethernet21/1 (Ethernet80, 25G) | 10.0.10.225/24 | up |

### Hare-lorax LLDP summary

| hare-lorax Port | Alias | Speed | LLDP Neighbor | Remote Interface | Status |
|-----------------|-------|-------|---------------|------------------|--------|
| Ethernet0  | Ethernet1/1  | 25G  | test-et6b3  | ens1f1np1 | up |
| Ethernet1  | Ethernet1/2  | 25G  | test-et8b4  | ens1f1np1 | up |
| Ethernet16 | Ethernet5/1  | 100G | rabbit-lorax | Ethernet13/1 | up |
| Ethernet20 | Ethernet6/1  | 50G  | test-et6b1  | ens3f0np0 | up |
| Ethernet22 | Ethernet6/3  | 50G  | test-et6b3  | ens3f0np0 | up |
| Ethernet24 | Ethernet7/1  | 100G | test-et6b3  | ens3f1np1 | up |
| Ethernet28 | Ethernet8/1  | 100G | test-et6b1  | ens3f1np1 | up |
| Ethernet32 | Ethernet9/1  | 100G | rabbit-lorax | Ethernet14/1 | up |
| Ethernet48 | Ethernet13/1 | 100G | rabbit-lorax | Ethernet15/1 | up |
| Ethernet66 | Ethernet17/3 | 25G  | test-et7b3  | ens1f1np1 | up |
| Ethernet67 | Ethernet17/4 | 25G  | test-et25b1 | ens1f1np1 | up |
| Ethernet80 | Ethernet21/1 | 25G  | test-et7b1  | ens1f1np1 | up |
| Ethernet84 | Ethernet22/1 | 100G | rabbit-lorax | Ethernet22/1 | up |
| Ethernet108| Ethernet28/1 | 100G | rabbit-lorax | Ethernet28/1 | up |
| Ethernet112| Ethernet16/1 | 100G | rabbit-lorax | Ethernet16/1 | up |

**Notes:**
- The 2×50G breakout sub-ports (Ethernet20/22) were admin-down after DPB. Fixed with
  `config interface startup Ethernet20/22` + autoneg enabled; now up/up. (verified 2026-04-07)
- test-et25b1 has a CX6 Dx NIC with both ports down — not cabled to hare-lorax yet.
- test-et6b1 CX4 port 1 (ens1f1np1) is down — not cabled.
