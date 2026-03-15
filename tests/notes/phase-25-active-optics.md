# Phase 25: Active Optics / Media Settings

**Date:** 2026-03-14
**Status:** Blocked — physical fiber/module issue preventing link-up

---

## Hardware Inventory

Two optical QSFP28 ports on the Wedge 100S-32X are connected (or intended to be connected)
to the Arista EOS peer at 192.168.88.14:

| SONiC port | Port # | BCM port | I2C bus | Alias | EOS peer |
|-----------|--------|----------|---------|-------|----------|
| Ethernet104 | 27 | ce26 (port 118) | 29 | Ethernet27/1 | Et27/1 |
| Ethernet108 | 28 | ce27 (port 122) | 28 | Ethernet28/1 | Et28/1 |

### EOS-Side Module Info (reliable — no mux contention on EOS)

| EOS port | EOS module type | Vendor | Serial | TX power |
|----------|-----------------|--------|--------|----------|
| Et27/1 | 100GBASE-CWDM4 | FINISAR CORP. | U4EA2RE | +1.12 dBm |
| Et28/1 | 100GBASE-CWDM4 | ColorChip ltd | (read from EOS) | −30.00 dBm (squelched) |

### SONiC-Side Module Info (unreliable — EEPROM corrupted by mux contention)

STATE_DB Ethernet104 shows: type="QSFP28 or later", manufacturer="@@@@@" (garbage),
spec_compliance="100GBASE-SR4 or 25GBASE-SR" (likely wrong due to corrupted EEPROM bytes).
True SONiC-side module type unknown until Phase 1 of EOS-LIKE-PLAN is deployed (i2c daemon).

---

## Link Status (verified on hardware 2026-03-14)

Both links have been DOWN since SONiC was first installed (~52 days):

```
SONiC show interfaces status:
  Ethernet104  93,94,95,96  100G  9100  rs  Ethernet27/1  routed  down  up  QSFP28 or later
  Ethernet108  89,90,91,92  100G  9100  rs  Ethernet28/1  routed  down  up  QSFP28 or later

EOS show interfaces status:
  Et27/1  notconnect  1  full  100G  100GBASE-CWDM4
  Et28/1  notconnect  1  full  100G  100GBASE-CWDM4
```

---

## Diagnostic Findings

### EOS Transceiver DOM (2026-03-14)

| Parameter | Et27/1 (Finisar) | Et28/1 (ColorChip) |
|-----------|-----------------|---------------------|
| Temperature | 30.25°C | 32.32°C |
| Voltage | 3.27V | 3.23V |
| Laser current | 37.97 mA | 53.46 mA |
| **Tx Power** | **+1.12 dBm** (healthy) | **−30.00 dBm** (squelched/LOS) |
| **Rx Power** | **−30.00 dBm** (no signal from SONiC) | **−30.00 dBm** (no signal from SONiC) |

Key interpretation:
- Et27/1 (Finisar): laser is on, healthy, transmitting to SONiC. Receiving nothing from SONiC.
- Et28/1 (ColorChip): laser squelched (Tx=−30 dBm) because it receives no signal from SONiC
  (many CWDM4 modules squelch TX output when CDR is not locked on RX = no LOS-override)

### SONiC BCM PHY Diagnostics — ce26 (Ethernet104)

```
sudo bcmcmd "phy diag ce26 dsc"
```

```
LN  SD  LCK  RXPPM  CLK90  CLKP1  PF(M,L)  VGA  DCO  P1mV  M1mV  DFE(1..6)      TXPPM  TXEQ(n1,m,p1,2,3)  TXAMP  EYE       LINK_TIME
 0   1*  1*    6     41     0      0,0       3    9    193   129   51,2,1,0,0,-6   0      8, 60,44, 0, 0       8,0   0,0,0,0   122.8
 1   0   0     0     32     0      10,0      39   0    0     0     0,0,0,0,0,0     0      4, 64,44, 0, 0       8,0   0,0,0,0     0.0
 2   0   0     0     32     0      10,0      39   0    0     0     0,0,0,0,0,0     0      4, 64,44, 0, 0       8,0   0,0,0,0     0.0
 3   0   0     0     32     0      10,0      39   0    0     0     0,0,0,0,0,0     0      4, 64,44, 0, 0       8,0   0,0,0,0     0.0
```

Interpretation:
- Lane 0: SD=1 (signal detect), CDR=locked — EOS Finisar IS transmitting to SONiC lane 0
- Lanes 1–3: SD=0, CDR=unlocked — only 1 of 4 electrical lanes has signal from EOS
- Lane 0 TXEQ adapted (n1=8 vs default 4) — lane 0 has negotiated TX equalization
- LINK_TIME=122.8s on lane 0 — CDR has been locked for ~2 minutes

For a CWDM4 module (single fiber pair, 4 wavelengths multiplexed), ALL 4 electrical lanes
should show signal if the RX fiber is connected. Getting only lane 0 suggests either:
1. SONiC-side module is SR4 (parallel fiber) with only one fiber pair connected, OR
2. CWDM4 DEMUX in SONiC-side module is partially non-functional (unlikely for all 3 lanes), OR
3. Optical power from EOS is marginal — only the first wavelength is above threshold

### SONiC BCM PHY Diagnostics — ce27 (Ethernet108)

All 4 lanes SD=0, LCK=0, LINK_TIME=0.0 — no signal on any lane. EOS Et28/1 Tx=−30 dBm
(squelched) explains this: no light is reaching SONiC Ethernet108 from EOS.

### EOS PHY Detail — Et27/1

```
show interfaces Et27/1 phy detail (key fields):
  Forward Error Correction    Reed-Solomon        ← RS-FEC (CL91), matches SONiC
  FEC alignment lock          unaligned           ← not getting valid RS-FEC from SONiC
  PMA/PMD RX signal detect    no signal           ← EOS receives ZERO light from SONiC
  PMA/PMD lane RX signal detect: Lane 0–3 all "no signal"
  Tx Equalization: pre1=4, main=60, post1=48 (all lanes)
```

FEC matches (both sides RS-FEC CL91). The issue is pre-FEC: zero light reaching EOS from SONiC.

### BCM PRBS Test

`phy diag ce26 prbs set p=3` confirmed SONiC serdes IS generating TX signal electrically.
EOS continued to show "no signal" on all PMA/PMD lanes simultaneously — confirming the
SONiC optical module TX output is not reaching EOS, not a serdes output problem.

---

## Root Cause Assessment

### Confirmed

1. SONiC BCM serdes ce26 IS generating TX signal electrically (PRBS confirmed)
2. EOS Et27/1 Finisar module IS transmitting (+1.12 dBm, healthy)
3. EOS→SONiC fiber path exists for at least one lane (ce26 lane 0 SD=1, CDR locked)
4. SONiC→EOS fiber path NOT working: EOS sees zero light from SONiC on all 4 lanes

### Probable Causes (in order of likelihood)

1. **SONiC-side module TX squelching**: If the SONiC-side QSFP28 module asserts TXDIS
   or squelches laser due to LOS on its own RX lanes (lanes 1–3 have no signal), some
   modules turn off TX. This creates a deadlock: neither side can initiate.
   - *Fix: verify TXDIS state (QSFP register byte 86) and LOS override settings*

2. **SONiC-side module TX fiber not connected to EOS Et27/1**: The fiber that carries
   SONiC's TX light may go somewhere other than EOS Et27/1 RX, or may be disconnected.
   - *Fix: physical fiber trace from SONiC Ethernet104 TX connector to its destination*

3. **Incompatible module types**: If SONiC-side module is SR4 (850nm, multi-mode) and
   EOS Et27/1 is CWDM4 (1270–1330nm, single-mode), the fiber types and wavelengths are
   incompatible. Only a fortuitous partial connection could explain ce26 lane 0 having signal.
   - *Fix: identify SONiC-side module type (requires Phase 1 EOS-LIKE-PLAN or visual inspection)*

4. **Serdes preemphasis wrong for optical module**: BCM config uses preemphasis tuned for
   DAC cables. Optical modules with no CDR may require lower TX equalization.
   - *Partial fix: media_settings.json — see below (needed regardless but not sufficient alone)*

---

## What LLDP Needs

LLDP is protocol-agnostic in SONiC and runs automatically on any link-up port. No
special LLDP configuration is needed for optical ports. The sole requirement is:

**Link must be operationally UP** (both PMA CDR locked on all 4 lanes, FEC aligned, PCS up)

Once Ethernet104/108 come up, LLDP will discover EOS Et27/1/Et28/1 within 30s and
they will appear in `show lldp table`.

---

## media_settings.json (Preparatory Work)

Even if physical fiber issues prevent link-up today, creating `media_settings.json` is
the right first step for optical module support. It will be applied by xcvrd when Phase 1
(EOS-LIKE-PLAN i2c daemon) provides reliable EEPROM reads and module type identification.

### Format (from quanta IX8 reference)

```json
{
    "GLOBAL_MEDIA_SETTINGS": {
        "PORT_MEDIA_SETTINGS": {
            "27,28": {
                "100GBASE-SR4": {
                    "preemphasis": {
                        "lane0": "0x000000",
                        "lane1": "0x000000",
                        "lane2": "0x000000",
                        "lane3": "0x000000"
                    }
                },
                "100GBASE-CWDM4": {
                    "preemphasis": {
                        "lane0": "0x000000",
                        "lane1": "0x000000",
                        "lane2": "0x000000",
                        "lane3": "0x000000"
                    }
                }
            }
        }
    }
}
```

Correct preemphasis values for CWDM4/SR4 on BCM Tomahawk need to be sourced from:
- Finisar CWDM4 application note (PN FCLF-8521-3 or similar)
- BCM56960 (Tomahawk) optical module application note
- Reference SONiC platform with same module type (AS7712-32X uses QSFP+ SR4; check its BCM config)

For optical modules (no copper trace equalization needed): typical setting is
preemphasis = 0x000000 (zero pre/post cursor, maximum main cursor) or minimal pre-emphasis.
The BCM config `serdes_preemphasis` for lanes 89–96 is currently `0x284800` which may
have excessive pre-cursor (0x28=40) that could distort the optical waveform.

---

## Immediate Action Items

1. **Physical inspection** (requires access to hardware rack):
   - Identify SONiC-side modules in Ethernet104 and Ethernet108 visually (label/color code)
   - Trace fiber from SONiC Ethernet104 TX jack to its destination patch panel port
   - Verify fiber reaches EOS Et27/1 RX (not some other port or disconnected)

2. **TXDIS check** (requires Phase 1 i2c daemon or brief pmon stop):
   - Read QSFP byte 86 from bus 29 (Ethernet104) and bus 28 (Ethernet108)
   - Bits 3:0 = TXDIS for lanes 0–3; any 1 = laser disabled for that lane
   - `sudo dd if=/sys/bus/i2c/devices/29-0050/eeprom bs=1 skip=86 count=1 2>/dev/null | xxd`

3. **TX optical power measurement** (requires Phase 1 i2c daemon for DOM):
   - Read DOM TX power bytes 50–57 (lanes 0–3) from Ethernet104/108 modules
   - Expected for healthy optical module: > −8 dBm per lane
   - If all −30 dBm: TXDIS asserted or module defective

4. **Create media_settings.json** for ports 27–28 once correct preemphasis values are determined.

5. **LLDP test** — once link comes up:
   ```
   show lldp table  # should show Ethernet104 → rabbit-lorax Et27/1, Ethernet108 → Et28/1
   ```

---

## BCM Serdes Parameters (Current vs Required)

### Current (BCM config, lanes 89–96)
```
serdes_preemphasis_89.0=0x284800   # lane encoding: post=0x28=40, main=0x48=72, pre=0x00=0
serdes_preemphasis_90.0=0x284800
...
serdes_preemphasis_96.0=0x284800
```

### BCM DSC Live Reading (ce26 lane 0, adapted state)
```
TXEQ(n1,m,p1,2,3) TXAMP: 8, 60, 44, 0, 0    8,0
```
n1=pre1=8, m=main=60, p1=post1=44 — these are the RUNTIME serdes equalization values
after BCM runtime adaptation (may differ from static BCM config settings).

### Recommended for Optical Modules (CWDM4/SR4)
- Pre-cursor: 0 (no leading equalization)
- Main cursor: maximum (60–80)
- Post-cursor: 0 (no trailing equalization)
- Optical modules handle signal equalization internally; host-side emphasis makes things worse

Apply live for testing:
```bash
sudo bcmcmd "phy diag ce26 txeq n1=0 m=80 p1=0"
sudo bcmcmd "phy diag ce27 txeq n1=0 m=80 p1=0"
```
Then check EOS Et27/1 RX power; if it rises from −30 dBm, serdes settings were the issue.
