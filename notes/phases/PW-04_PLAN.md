# PW-04 — Active Optics: Plan

## Problem Statement

Ethernet104 (BCM ce26, I2C bus 29) and Ethernet108 (BCM ce27, I2C bus 28) have QSFP28
modules installed and are connected (via fiber) to Arista EOS rabbit-lorax (`192.168.88.14`)
ports Et27/1 and Et28/1. Both links have been DOWN on both sides since SONiC was first
installed (~52 days).

**This is a confirmed physical issue, not a SONiC software problem.** SONiC BCM serdes
is generating TX signal electrically (confirmed via PRBS test). The fiber path from SONiC
to EOS is broken.

Full diagnosis is in `notes/BEWARE_OPTICS.md` §1 and
`tests/notes/phase-25-active-optics.md`.

## Root Cause Summary

1. EOS Et27/1 (Finisar CWDM4): Tx=+1.12 dBm (healthy), Rx=−30 dBm (receives zero from SONiC)
2. EOS Et28/1 (ColorChip CWDM4): Tx=−30 dBm (laser squelched — no RX signal), Rx=−30 dBm
3. SONiC BCM ce26: lane 0 SD=1 (EOS fiber IS reaching SONiC), lanes 1–3 SD=0 (partial signal)
4. PRBS confirmed: SONiC serdes generates TX, but EOS sees zero light on all 4 lanes

**Probable physical causes (not yet resolved):**
- SONiC-side module TX fiber not physically connected to EOS Et27/1 RX port (most likely)
- SONiC-side module may be SR4 (850 nm MMF) rather than CWDM4 (1270–1330 nm SMF),
  making wavelength/fiber mismatch; module type is unconfirmed (EEPROM corrupted by I2C contention)
- SONiC-side module asserting TXDIS (byte 86, bits 3:0) due to partial LOS on RX

## Physical Checks Required Before Software Work

These must be completed by someone with physical access to the rack:

1. **Visual module ID**: Identify the label/color of modules in Ethernet104 and Ethernet108.
   CWDM4 modules are typically labeled and have a different bail color than SR4.

2. **TXDIS check** (can be done remotely if I2C contention issue is resolved by PW-EOS-I2C):
   ```bash
   sudo dd if=/sys/bus/i2c/devices/29-0050/eeprom bs=1 skip=86 count=1 2>/dev/null | xxd
   # bits 3:0 of byte 86 = TX disable per lane; 0x0f = all lanes disabled
   ```

3. **Fiber trace**: Physically trace fiber from SONiC Ethernet104 TX output to its
   destination at the patch panel or direct connect. Confirm it terminates at EOS Et27/1 RX.

4. **Module swap**: If modules are SR4 and EOS is CWDM4, replace SONiC-side modules
   with CWDM4 (matching EOS side).

## Proposed Software Approach (Post Physical Fix)

Once the physical issue is resolved and both modules can be positively identified as CWDM4:

### Step 1: Apply serdes preemphasis for optical modules

Optical modules do not need copper-style TX pre-emphasis. The current BCM config applies
`serdes_preemphasis=0x284800` (post=40, main=72, pre=0) which is tuned for DAC cables.
For CWDM4, the recommended setting is pre=0, main=maximum (60–80), post=0.

Apply live (does not survive reboot):
```bash
sudo bcmcmd "phy diag ce26 txeq n1=0 m=80 p1=0"
sudo bcmcmd "phy diag ce27 txeq n1=0 m=80 p1=0"
```

If this brings EOS RX power up, make permanent via `media_settings.json` (see PW-06).

### Step 2: Verify FEC mode

Both sides must use RS-FEC (CL91). SONiC config already has `fec=rs` for Ethernet104/108.
Verify:
```bash
show interfaces status Ethernet104 Ethernet108
# Column 5 should be "rs"
```

### Step 3: Monitor link-up

After physical fix and serdes adjustment:
```bash
watch -n 2 'show interfaces status Ethernet104 Ethernet108'
```
Expect both ports to transition to `up` within 10 seconds of optical path being established.

## Files to Change

No platform code changes needed for the physical fix. Software changes are in PW-06
(media_settings.json). This phase is about diagnosis and physical remediation.

## Acceptance Criteria

- Ethernet104 shows `oper: up` in `show interfaces status`
- Ethernet108 shows `oper: up` in `show interfaces status`
- EOS Et27/1 and Et28/1 show `connected` in `show interfaces status`
- `show interfaces transceiver eeprom Ethernet104` shows CWDM4 module type
  (requires I2C daemon from EOS-like plan for reliable EEPROM reads)
- DOM RX power > −10 dBm on both SONiC ports and EOS ports

## Risks

- Module type mismatch (SR4 vs CWDM4) may require purchasing new modules
- Fiber routing in the rack may require cable rework
- Even after physical fix, serdes preemphasis tuning (PW-06) may be needed for link stability
- Do not cycle power on the SONiC switch or remove modules while pmon/xcvrd is running —
  see BEWARE_OPTICS.md for I2C bus hang risk
