# Link Status Investigation — 2026-03-04

## Symptom
All 32 QSFP ports show Oper:down with N/A type in `show interface status`.
BCM shell `ps` showed 128 ports (flex config): 32 ce "down", 96 xe "!ena".
All ce ports had KR4 interface type — DAC cables require CR4.

## Root Cause (verified 2026-03-04)

### Issue 1: Forced fiber/KR4 mode in BCM config
The Wedge100S BCM config (derived from Facebook Wedge100) explicitly set:
- `serdes_fiber_pref=0x1` — forces fiber (KR4) interface type
- `phy_an_c73=0x0` — disables CL73 autoneg (needed for 100G CR4 negotiation)
- `serdes_automedium=0x0` — disables auto medium detection

The AS7712-32X (known-working TH1 32x100G platform) has **none** of these settings,
letting the SDK defaults handle both copper DAC (CR4) and optical (KR4).

With DAC cables, KR4 won't bring up the link. Since autoneg and auto-medium
are disabled, the SDK cannot discover the cable is copper and switch to CR4.

### Issue 2: sai.profile pointed to flex config
`sai.profile` loaded `th-wedge100s-32x-flex.config.bcm` (128 portmap entries).
This created 128 BCM ports when SONiC only expects 32 (per `port_config.ini`).
The non-flex `th-wedge100s-32x100G.config.bcm` creates exactly 32 ports.

## Fixes Applied

1. **Switched sai.profile** to `th-wedge100s-32x100G.config.bcm` (non-flex, 32 ports)
2. **Removed from non-flex BCM config:**
   - `phy_an_c73=0x0` (let SDK default — enables CL73 autoneg for 100G)
   - `serdes_automedium=0x0` (let SDK default — enables auto medium detection)
   - `serdes_fiber_pref=0x1` (let SDK default — copper preference)
3. **Kept:** `phy_an_c37=0x3` (CL37 autoneg for lower speeds, harmless)
4. **Kept:** All serdes_preemphasis and xgxs lane map values (board-specific PCB trace tuning)

## Config Comparison

| Setting | Facebook Wedge100 | Wedge100S (before) | Wedge100S (after) | AS7712-32X |
|---|---|---|---|---|
| phy_an_c73 | 0x0 | 0x0 | (removed) | (absent) |
| serdes_automedium | 0x0 | 0x0 | (removed) | (absent) |
| serdes_fiber_pref | 0x1 | 0x1 | (removed) | (absent) |
| serdes_preemphasis | per-port | per-port (same) | per-port (same) | per-lane |
| xgxs lane maps | per-port | per-port (same) | per-port (same) | per-port |
| serdes_driver_current | absent | absent | absent | per-lane (0x8) |

## Notes
- Facebook Wedge100 config likely designed for optics (fiber), not DAC
- The serdes preemphasis and lane maps are identical between FB Wedge100 and Wedge100S
  (same chassis/PCB design)
- `serdes_driver_current` absent in FB/Wedge100S but present in AS7712 (0x8 per lane);
  SDK defaults should be adequate for initial bring-up
- `port_breakout_config_db.json` defines 128 ports at 25G (full 4x25G breakout template);
  this is a DPB reference, not loaded at boot — not an issue
- ONL repo has no BCM config files for wedge100 platforms

## Files Changed
- `device/.../Accton-WEDGE100S-32X/sai.profile` — point to non-flex config
- `device/.../Accton-WEDGE100S-32X/th-wedge100s-32x100G.config.bcm` — remove forced fiber settings

## Next Steps
- Deploy updated BCM config to target and restart syncd (or reboot)
- Verify BCM `ps` shows 32 ports (not 128)
- Check if any ports come up with DAC cables
- If still no link, investigate adding `serdes_driver_current` per-port settings
