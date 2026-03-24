# PW-06 — Media Settings: Test Plan

## Overview

Verify that `media_settings.json` is deployed, read by xcvrd without error, and
does not regress currently-working DAC port links. Optical port improvement is
conditional on PW-04 physical fix being complete.

## Required Hardware State

- SONiC running on Wedge 100S-32X (`192.168.88.12`)
- pmon running (xcvrd must be active)
- DAC ports Ethernet16, 32, 48, 112 must be up before the test (baseline)
- `media_settings.json` deployed to
  `device/accton/x86_64-accton_wedge100s_32x-r0/media_settings.json`
  and included in the installed platform package

## Dependencies

- Platform package (.deb) must be rebuilt and installed after adding `media_settings.json`
- PW-04 physical fix is required for optical port pass criteria (T6–T8)
- EOS-like I2C daemon recommended for T7 (reliable module type identification)

---

## Test Actions

### T1: File is present on the switch

```bash
ssh admin@192.168.88.12 \
  ls -la /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/media_settings.json
```

**Pass:** File exists with non-zero size.

### T2: File is valid JSON

```bash
ssh admin@192.168.88.12 \
  python3 -c "import json; json.load(open('/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/media_settings.json')); print('valid JSON')"
```

**Pass:** Prints `valid JSON`.

### T3: xcvrd reads file without error

```bash
ssh admin@192.168.88.12 sudo grep -i "media_settings" /var/log/syslog | tail -5
```

**Pass:** No `ERROR` or `WARNING` lines from xcvrd mentioning `media_settings`.
If xcvrd logged `Loaded media settings` or similar, that is a positive indicator.

### T4: DAC port regression check — Ethernet16/32/48/112 still up

```bash
ssh admin@192.168.88.12 show interfaces status Ethernet16 Ethernet32 Ethernet48 Ethernet112
```

**Pass:** All four ports show `oper: up`.
**Fail:** Any port drops to `down` after xcvrd restart with new `media_settings.json`.

This is the critical safety check. Run immediately after deploying the file and
restarting pmon.

### T5: Serdes preemphasis not changed for DAC ports

```bash
# Check BCM DSC for a DAC port (ce0 = Ethernet0, ce1 = Ethernet4, etc.)
ssh admin@192.168.88.12 sudo bcmcmd "phy diag ce4 dsc" 2>/dev/null | head -5
```

**Pass:** Lane TXEQ values for DAC ports are unchanged from pre-deployment baseline.
Note the baseline values before deploying `media_settings.json`.

### T6: ASIC_DB reflects media_settings values for ports 27/28 (conditional on I2C daemon)

```bash
ssh admin@192.168.88.12 bash -c '
  PORT_OID=$(redis-cli -n 1 hget COUNTERS_PORT_NAME_MAP Ethernet104)
  redis-cli -n 1 hget "ASIC_STATE:SAI_OBJECT_TYPE_PORT:${PORT_OID}" SAI_PORT_ATTR_SERDES_PREEMPHASIS
'
```

**Pass:** Returns a value consistent with the preemphasis specified in `media_settings.json`.
**Note:** This only works if xcvrd successfully identified the module type from EEPROM.
May show the default value if EEPROM reads are unreliable (I2C contention).

### T7: Live bcmcmd confirms optical port serdes settings applied

```bash
ssh admin@192.168.88.12 sudo bcmcmd "phy diag ce26 dsc" 2>/dev/null | head -5
ssh admin@192.168.88.12 sudo bcmcmd "phy diag ce27 dsc" 2>/dev/null | head -5
```

**Pass:** TXEQ for ce26/ce27 shows reduced post-cursor compared to DAC port values.
Expected: `n1=0, p1=0` (pre=0, post=0) if CWDM4 settings were applied.
Baseline (DAC tuned): `n1=8, m=60, p1=44` (from phase-25 diagnosis).

### T8: Optical port link-up after PW-04 physical fix (conditional)

*Only run after PW-04 is complete.*

```bash
ssh admin@192.168.88.12 show interfaces status Ethernet104 Ethernet108
```

**Pass:** Both ports show `oper: up`.

---

## Pass/Fail Criteria Summary

| Test | Pass condition | Required? |
|---|---|---|
| T1 | `media_settings.json` present on switch | Yes |
| T2 | File is valid JSON | Yes |
| T3 | No xcvrd errors reading the file | Yes |
| T4 | DAC ports Ethernet16/32/48/112 remain up | Yes — critical regression check |
| T5 | DAC port serdes unchanged | Yes |
| T6 | ASIC_DB shows new preemphasis for ports 27/28 | Conditional (I2C daemon required) |
| T7 | bcmcmd confirms optical serdes settings | Conditional (PW-04 required) |
| T8 | Optical ports link-up | Conditional (PW-04 required) |

T1–T5 must pass for PW-06 to be considered complete regardless of PW-04 status.
T6–T8 are additional validation once the physical blocker is resolved.
