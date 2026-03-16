# BEWARE: Optical Links, Autoneg, and FEC — Wedge 100S-32X

> **Read this before touching autoneg, FEC, or optical port configuration.**
> Violations cause silent misconfiguration or permanent link-down states.

## DANGER SUMMARY

| Topic | Danger |
|---|---|
| Ethernet104/108 | Both DOWN since install (~52 days). Confirmed physical issue. Not a software problem. Do not spend debugging time on software. |
| Autoneg | SONiC CLI accepts it. SAI does NOT program the ASIC. `SAI_PORT_ATTR_AUTO_NEG_MODE` stays `false`. Silent no-op. |
| FC-FEC (CL74) | Rejected by SONiC CLI for 100G ports. Not supported on Tomahawk BCM56960. |
| RS-FEC (CL91) | Required for 100GBASE-CR4. Must be set explicitly — AN does not negotiate it. |

---

## 1. Ethernet104/108 CWDM4 Modules — Physical Blocker

**Ports:** Ethernet104 (BCM ce26, I2C bus 29) and Ethernet108 (BCM ce27, I2C bus 28).
**Peer:** Arista EOS rabbit-lorax (`192.168.88.14`) Et27/1 and Et28/1.
**Status:** DOWN on both sides since SONiC was first installed (verified 2026-03-14).

### Link status at time of diagnosis

```
# SONiC
show interfaces status Ethernet104 Ethernet108
  Ethernet104  93,94,95,96  100G  9100  rs  Ethernet27/1  routed  down  up  QSFP28 or later
  Ethernet108  89,90,91,92  100G  9100  rs  Ethernet28/1  routed  down  up  QSFP28 or later

# EOS (rabbit-lorax)
show interfaces status Et27/1 Et28/1
  Et27/1  notconnect  1  full  100G  100GBASE-CWDM4
  Et28/1  notconnect  1  full  100G  100GBASE-CWDM4
```

### Diagnostic commands run to confirm physical cause

```bash
# EOS DOM (reliable — no I2C mux contention on EOS)
show interfaces Et27/1 transceiver detail
show interfaces Et28/1 transceiver detail

# SONiC BCM PHY diagnostics
sudo bcmcmd "phy diag ce26 dsc"
sudo bcmcmd "phy diag ce27 dsc"

# PRBS test to confirm serdes is generating TX signal
sudo bcmcmd "phy diag ce26 prbs set p=3"
# Then check EOS Et27/1 RX signal detect
show interfaces Et27/1 phy detail
```

### Key findings

EOS DOM for Et27/1 (Finisar CWDM4): Tx=+1.12 dBm (healthy laser), Rx=−30.00 dBm (receives nothing from SONiC).
EOS DOM for Et28/1 (ColorChip CWDM4): Tx=−30.00 dBm (squelched — CDR not locked on RX), Rx=−30.00 dBm.

BCM DSC for ce26: only lane 0 shows `SD=1, LCK=1` (signal detect, CDR locked). Lanes 1–3 are dead.
For a CWDM4 module (4 wavelengths on one fiber pair), all 4 lanes must have signal — only lane 0 having
signal points to either a mismatched module type (SR4 with partial fiber) or a severely marginal optical path.

PRBS test: `phy diag ce26 prbs set p=3` confirmed SONiC serdes IS generating TX electrically.
EOS Et27/1 continued to report `PMA/PMD RX signal detect: no signal` on all 4 lanes simultaneously —
proving SONiC's optical TX is not reaching EOS. This is not a serdes problem.

EOS FEC state for Et27/1: `Forward Error Correction: Reed-Solomon`, `FEC alignment lock: unaligned`.
FEC mode matches (both sides RS-FEC CL91). The failure is pre-FEC: zero photons reaching EOS.

**Conclusion:** SONiC serdes works. Fiber path SONiC→EOS is broken or SONiC-side module TX is
disabled (TXDIS) or incompatible with CWDM4 wavelengths. Physical issue. Do not attempt software fixes.

### Remaining physical checks (not yet done)

- Check TXDIS (QSFP byte 86, bits 3:0 = lanes 0–3): `sudo dd if=/sys/bus/i2c/devices/29-0050/eeprom bs=1 skip=86 count=1 2>/dev/null | xxd`
- Visual ID of SONiC-side module type (SR4 vs CWDM4 label/color)
- Physical fiber trace from SONiC Ethernet104 TX jack to its destination
- Note: SONiC-side EEPROM is unreliable (I2C mux contention); do not trust STATE_DB `TRANSCEIVER_INFO|Ethernet104` module type

---

## 2. Auto-Negotiation

**Hardware-verified 2026-03-02.**

### What works

```bash
config interface autoneg Ethernet16 enabled   # rc=0
show interfaces autoneg status Ethernet16     # shows "enabled"
```

CONFIG_DB and APP_DB both propagate the setting correctly.

### What does NOT work

ASIC_DB `SAI_PORT_ATTR_AUTO_NEG_MODE` remains `false` after enabling AN. Verified:

```bash
redis-cli -n 1 hget "$(redis-cli -n 1 keys 'ASIC_STATE:SAI_OBJECT_TYPE_PORT:*' | head -1)" SAI_PORT_ATTR_AUTO_NEG_MODE
# Returns: false
```

No errors in syncd or swss logs — the SAI silently ignores the request.

### Why it is broken at ASIC level

The BCM config has Clause 73 AN explicitly disabled:

```
# device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm
phy_an_c73=0x0     # Clause 73 (IEEE 802.3ap backplane/DAC AN) — DISABLED
phy_an_c37=0x3     # Clause 37 (1G Ethernet AN) — enabled but irrelevant for QSFP28
```

The Broadcom SAI returns success for `SAI_PORT_ATTR_AUTO_NEG_MODE=true` but cannot override
`phy_an_c73=0x0`. AS7712-32X and AS7716-32X do not set `phy_an_c73` (rely on BCM SDK defaults).

**Do NOT change `phy_an_c73` without extensive testing** — it affects all ports, and
RS-FEC negotiation via CL73 is untested on this platform.

Since AN does not reach hardware, `adv_speeds` and `adv_interface_types` are also no-ops.
RS-FEC must be set explicitly on every 100G port that needs it (see §3).

### BCM port state confirming no AN

```
# sudo bcmcmd "ps"  (admin-up port example)
ce0(1)   down 4 100G FD SW No Forward Untag FA KR4 9122
```

`KR4` = static mode, no `AN` flag present. This is expected and correct with `phy_an_c73=0x0`.

---

## 3. FEC

**Hardware-verified 2026-03-02.**

### RS-FEC (CL91) — required, works

```bash
config interface fec Ethernet16 rs
```

- CONFIG_DB: `fec=rs` — propagates correctly
- APP_DB: `fec=rs` — propagates correctly
- ASIC_DB: `SAI_PORT_ATTR_FEC_MODE=SAI_PORT_FEC_MODE_RS` — confirmed

All four DAC-connected ports (Ethernet16, 32, 48, 112) came UP immediately after `fec rs`.
Before that, BCM DSC showed `SD=1, LCK=1` but Arista reported `FEC alignment lock: unaligned,
MAC Rx Local Fault: true`. Root cause: CL73 AN is disabled so FEC is never negotiated — must be static.

Verify via ASIC_DB:
```bash
PORT_OID=$(redis-cli -n 1 hget COUNTERS_PORT_NAME_MAP Ethernet16)
redis-cli -n 1 hget "ASIC_STATE:SAI_OBJECT_TYPE_PORT:${PORT_OID}" SAI_PORT_ATTR_FEC_MODE
# Expected: SAI_PORT_FEC_MODE_RS
```

### FC-FEC (CL74) — rejected, not supported for 100G

```bash
config interface fec Ethernet16 fc
# Error: fec fc is not in ['none', 'rs']
```

FC-FEC (FireCode, CL74) is the 25G/10G FEC mode. The Tomahawk SAI restricts 100G ports to
`rs` and `none`. If 4x25G breakout is deployed (Phase 14b), CL74 behavior on sub-ports is
untested and may also be rejected — verify before relying on it.

### No FEC

```bash
config interface fec Ethernet16 none
# ASIC_DB: SAI_PORT_ATTR_FEC_MODE=SAI_PORT_FEC_MODE_NONE  — works
```

Use `none` only for loopback testing or when the peer explicitly disables FEC.
Arista EOS uses RS-FEC on all 100G DAC ports by default.

---

## 4. Optical Serdes Preemphasis (Informational)

The BCM config sets `serdes_preemphasis=0x284800` for lanes 88–96 (ports 27–28, Ethernet104/108).
Encoding: post=0x28 (40), main=0x48 (72), pre=0x00. This is tuned for copper DAC, not optical.

Optical modules handle equalization internally; excessive host-side pre/post cursor distorts the
waveform. Recommended for CWDM4/SR4: pre=0, main=max (60–80), post=0.

Apply live for testing (does not survive reboot):
```bash
sudo bcmcmd "phy diag ce26 txeq n1=0 m=80 p1=0"
sudo bcmcmd "phy diag ce27 txeq n1=0 m=80 p1=0"
```

To make permanent, add `media_settings.json` for ports 27–28 in the device directory.
Preparatory work only; will not bring links up until the physical fiber issue is resolved.
