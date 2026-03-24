# PW-04 — Active Optics: Test Plan

## Overview

Verify Ethernet104 and Ethernet108 CWDM4 links reach operational UP state after the
physical fiber/module issue is resolved. All tests assume the physical remediation from
PW-04_PLAN has been completed.

## Required Hardware State

- SONiC running on Wedge 100S-32X (`192.168.88.12`)
- EOS running on rabbit-lorax (`192.168.88.14`)
- Physical fiber path SONiC Ethernet104 TX → EOS Et27/1 RX confirmed (and Ethernet108/Et28/1)
- Modules on both SONiC and EOS side confirmed as CWDM4 (or both SR4 — must match)
- TXDIS (QSFP byte 86) confirmed as 0x00 (all lanes enabled) on SONiC-side modules

## Dependencies

- Physical remediation complete (fiber trace, module verification)
- PW-06 (media_settings.json) is optional but recommended for optical preemphasis
- EOS-like I2C daemon (from EOS-LIKE-PLAN) recommended for reliable EEPROM reads,
  but not strictly required if physical issue is resolved

---

## Test Actions

### T1: EOS DOM confirms TX healthy on both ports

```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et27/1 transceiver detail | grep -E "Tx Power|Rx Power"'

sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces Et28/1 transceiver detail | grep -E "Tx Power|Rx Power"'
```

**Pass:**
- Et27/1 Tx Power > −5 dBm (was +1.12 dBm, should remain healthy)
- Et28/1 Tx Power > −5 dBm (was −30 dBm squelched; should recover when RX signal present)
- Both: Rx Power > −10 dBm (receiving light from SONiC)

**Fail:** Either port shows Rx Power < −20 dBm (fiber path still broken).

### T2: SONiC BCM serdes signal detect — all 4 lanes

```bash
ssh admin@192.168.88.12 sudo bcmcmd "phy diag ce26 dsc" 2>/dev/null | head -10
ssh admin@192.168.88.12 sudo bcmcmd "phy diag ce27 dsc" 2>/dev/null | head -10
```

**Pass:** All 4 lanes show `SD=1, LCK=1` for both ce26 and ce27.
**Current (broken) state:** Only ce26 lane 0 shows SD=1; ce27 all lanes SD=0.

### T3: Interface operational status

```bash
ssh admin@192.168.88.12 show interfaces status Ethernet104 Ethernet108
```

**Pass:** Both ports show `oper: up` in the Oper column.

### T4: EOS interface link-up

```bash
sshpass -p '0penSesame' ssh -tt -o StrictHostKeyChecking=no \
  -J admin@192.168.88.12 admin@192.168.88.14 \
  'show interfaces status Et27/1 Et28/1'
```

**Pass:** Both ports show `connected` (not `notconnect`).

### T5: Transceiver info — module type identified (requires I2C daemon)

```bash
ssh admin@192.168.88.12 show interfaces transceiver eeprom Ethernet104 | grep -i "specification\|identifier\|vendor"
```

**Pass:** Shows CWDM4 in spec compliance (or CWDM4 vendor PN if module supports it).
**Note:** This test may be unreliable if the EOS-like I2C daemon is not deployed, due to
I2C mux contention on ports 27–28.

### T6: DOM RX power on SONiC side (requires active optics + I2C daemon)

```bash
ssh admin@192.168.88.12 show interfaces transceiver dom Ethernet104
ssh admin@192.168.88.12 show interfaces transceiver dom Ethernet108
```

**Pass:** RX power > −10 dBm on all 4 channels.
**Note:** Requires working I2C daemon for reliable DOM reads.

### T7: Link stability — no flapping over 5 minutes

```bash
ssh admin@192.168.88.12 bash -c '
  for i in $(seq 1 5); do
    sleep 60
    show interfaces status Ethernet104 Ethernet108 | grep -E "Ethernet10[48]"
  done
'
```

**Pass:** Both ports remain `up` for all 5 checks with no flapping.

---

## Pass/Fail Criteria Summary

| Test | Pass condition |
|---|---|
| T1 | EOS RX power > −10 dBm on both ports |
| T2 | All 4 lanes SD=1, LCK=1 on ce26 and ce27 |
| T3 | SONiC Ethernet104 and Ethernet108 show `up` |
| T4 | EOS Et27/1 and Et28/1 show `connected` |
| T5 | Module type identifies as CWDM4 (if I2C daemon available) |
| T6 | DOM RX power > −10 dBm (if I2C daemon available) |
| T7 | Links stable for 5 minutes without flapping |

T1–T4 are hard pass/fail requirements. T5–T6 are conditional on I2C daemon deployment.
T7 is required for phase completion.
