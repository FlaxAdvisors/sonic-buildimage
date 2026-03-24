# Ethernet116 Link-Down Root Cause: ColorChip CWDM4 Tx Fault

**Date:** 2026-03-20
**Status:** Hardware failure confirmed — transceiver replacement required

## Summary

Ethernet116 (SONiC) ↔ Et30/1 (Arista EOS, 192.168.88.14) link is down due to a **hardware Tx failure in the ColorChip CWDM4 transceiver** installed in SONiC port Ethernet116 (QSFP slot 29/30, alias Ethernet30/1).

The link failure is NOT a FEC mismatch — both sides use RS-FEC. The laser driver or laser diode has failed: Tx bias current is present (~113 mA) but Tx optical output power is at the noise floor (-30.0 dBm on all 4 lanes).

## Key Evidence

### Arista Et30/1 PHY Detail (verified on hardware 2026-03-20)

```
show interfaces Et30/1 phy detail
  PMA/PMD RX signal detect    no signal    (all 4 lanes)
  Forward Error Correction    Reed-Solomon
  Reed-Solomon codeword size  528
  FEC alignment lock          unaligned
  FEC corrected codewords     0
  FEC uncorrected codewords   0
  MAC Rx Local Fault          true
```

- Arista uses RS-FEC (528-bit codeword = standard 100G RS-FEC), matching SONiC `fec=rs`. **FEC mismatch is NOT the cause.**
- Arista detects zero signal from SONiC Tx on all 4 PMA lanes.
- Interface has been down for 58+ days.

### SONiC Ethernet116 Transceiver Status (verified on hardware 2026-03-20)

```
show interfaces transceiver status Ethernet116
  CMIS State (SW): READY
  Tx fault flag on media lane 1: True
  Tx fault flag on media lane 2: True
  Tx fault flag on media lane 3: True
  Tx fault flag on media lane 4: True
  Rx loss of signal flag on media lane 1: False  (Rx path is healthy)
  TX disable status on all lanes: False
  Disabled TX channels: 0

show interfaces transceiver pm Ethernet116
  Lane    Rx Power (dBm)    Tx Bias (mA)    Tx Power (dBm)
  1       0.777             113.17          -30.0
  2       -0.731            113.17          -30.0
  3       0.237             113.17          -30.0
  4       -0.11             113.17          -30.0
```

- Tx bias ~113 mA (laser is being driven) but Tx power = -30.0 dBm (noise floor, effectively zero)
- Rx signal is good (0 to -0.7 dBm inbound from Arista)
- Tx fault is asserted persistently on all 4 lanes

### QSFP EEPROM Raw (lower page, byte 4)

```
xxd /run/wedge100s/sfp_29_eeprom | head -1
00000000: 1107 0000 0f00 0000 ...
```

- Byte 4 = `0x0f` = `0b00001111` = Tx fault bits set for lanes 1, 2, 3, 4 (SFF-8636 table)
- Fault is latched and re-asserts immediately (persistent hardware failure)

### Transceiver Identity

| Field | Value |
|-------|-------|
| Vendor | ColorChip Ltd |
| Part Number | C100QSFPCWDM400B |
| Serial Number | 17314400 |
| Date Code | 2017-07-26 |
| Type | QSFP28 100G CWDM4 |
| Connector | LC |

### Reset Attempts Failed

- `sudo sfputil reset Ethernet116` → Failed (reset pin not accessible from host CPU)
- `sonic_platform` API `sfp.reset()` → Returns False (stub — not implemented for this platform)
- Admin shutdown/startup cycle on Ethernet116 → Tx fault persists
- `show interfaces transceiver error-status` → Reports "OK" (error-status only tracks EEPROM access issues, not Tx fault)

## Root Cause

The ColorChip CWDM4 optic (SN 17314400, manufactured 2017-07-26) has a hardware Tx failure. The laser driver provides bias current but the laser diode is not emitting optical power. The fault is persistent and non-recoverable without replacing the transceiver.

The Rx path is healthy (Arista is transmitting correctly, SONiC receives -0.7 to +0.7 dBm), confirming the fiber and the Arista side are fine.

## Why Initial Diagnosis Showed "Tx LOL=False, TXDIS=False"

- **TXDIS=False**: TX disable register is correctly not set — this is a software control, not the fault
- **Tx LOL (CDR lock)**: The CDR may have locked to the SerDes input signal; the CDR can be locked even if the laser is not emitting
- **Tx bias 113 mA**: The laser driver circuit is energized, but the actual laser diode may be open-circuit or degraded

## Resolution

**Replace the ColorChip CWDM4 transceiver in Ethernet116 (QSFP slot 29).** The peer side (Arista Et30/1) and fiber plant are functional.

After replacement:
- Confirm FEC is `rs` on SONiC: `show interfaces status Ethernet116` (already correct)
- Arista Et30/1 FEC is already Reed-Solomon — no config change needed on Arista
- Link should come up immediately with new optic

## FEC Alignment Confirmed (not a mismatch)

| Side | FEC Mode |
|------|----------|
| SONiC Ethernet116 | rs (RS-FEC) |
| Arista Et30/1 | Reed-Solomon (528-bit codeword, BCM56960-TSCF) |

Both sides match. No FEC configuration changes are needed.
