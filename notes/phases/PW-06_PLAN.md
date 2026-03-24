# PW-06 — Media Settings: Plan

## Problem Statement

SONiC supports per-port media settings via `media_settings.json` in the platform device
directory. When xcvrd identifies a transceiver type (via EEPROM), it reads this file and
programs BCM serdes TX parameters (pre-emphasis, driver current) via SAI attributes.

`device/accton/x86_64-accton_wedge100s_32x-r0/media_settings.json` **does not exist**.

All 32 ports currently use the static BCM config preemphasis values from
`th-wedge100s-32x-flex.config.bcm`. The relevant values for the optical ports (Ethernet104/108,
BCM lanes 89–96) are:
```
serdes_preemphasis_89.0=0x284800   # post=0x28=40, main=0x48=72, pre=0x00=0
```

This is a DAC-cable-tuned value. Optical modules (CWDM4, SR4) handle equalization
internally and perform better with minimal host-side TX emphasis:
- pre-cursor: 0
- main-cursor: 60–80
- post-cursor: 0

Without `media_settings.json`, xcvrd applies a single static preemphasis for all module
types, which may impair optical module links even after the physical issue (PW-04) is resolved.

## Reference Platforms

No `media_settings.json` exists for AS7712-32X in this tree. The Quanta IX8 (BCM Tomahawk,
same ASIC) at `device/quanta/x86_64-quanta_ix8_rglbmc-r0/media_settings.json` is the
closest reference.

## Proposed Approach

### File to Create

```
device/accton/x86_64-accton_wedge100s_32x-r0/media_settings.json
```

### Content Strategy

The file uses SONiC's `PORT_MEDIA_SETTINGS` format with port numbers (0-based from
`port_config.ini` lane index / 4) as keys.

**DAC cable ports (ports 0–26, 29–31):** These are currently working. They must not be
changed. Either omit them from the file (xcvrd falls back to BCM config defaults) or
explicitly set the existing preemphasis value.

**Optical ports (ports 27 and 28 = Ethernet104 and Ethernet108):** Set based on
CWDM4 optical requirements:

```json
{
    "GLOBAL_MEDIA_SETTINGS": {
        "PORT_MEDIA_SETTINGS": {
            "27,28": {
                "100GBASE-CWDM4": {
                    "preemphasis": {
                        "lane0": "0x004800",
                        "lane1": "0x004800",
                        "lane2": "0x004800",
                        "lane3": "0x004800"
                    }
                },
                "100GBASE-SR4": {
                    "preemphasis": {
                        "lane0": "0x004800",
                        "lane1": "0x004800",
                        "lane2": "0x004800",
                        "lane3": "0x004800"
                    }
                }
            }
        }
    }
}
```

Encoding `0x004800`: post=0x00, main=0x48=72, pre=0x00. This eliminates post-cursor
while preserving the BCM default main cursor.

The BCM DSC live reading from phase-25-active-optics.md showed lane 0 adapted to
`n1=8, m=60, p1=44` (runtime). The initial static attempt should reduce post-cursor
(0x28=40 → 0x00) first, then tune main cursor if needed.

### How xcvrd Applies media_settings.json

xcvrd reads the file on startup and on each transceiver insertion event. It matches
the module type string from the EEPROM (e.g., `"100GBASE-CWDM4"`) against the keys
in `PORT_MEDIA_SETTINGS` for the port number. If a match is found, it calls the SAI
to set `SAI_PORT_ATTR_SERDES_PREEMPHASIS`.

**Dependency on reliable EEPROM reads**: The module type string must be correctly
identified from EEPROM. Ethernet104/108 currently have corrupted EEPROM due to I2C
mux contention (STATE_DB shows manufacturer `@@@@@`). This means `media_settings.json`
will not take effect until the EOS-like I2C daemon is deployed (from EOS-LIKE-PLAN)
and provides clean EEPROM reads.

### Tuning Process

1. Deploy `media_settings.json` with initial CWDM4 values (post=0, main=72, pre=0)
2. Restart pmon/xcvrd: `sudo systemctl restart pmon`
3. Check if SAI applied the setting: look for `SAI_PORT_ATTR_SERDES_PREEMPHASIS` in ASIC_DB
4. Apply live adjustment if needed: `sudo bcmcmd "phy diag ce26 txeq n1=0 m=80 p1=0"`
5. Check EOS Et27/1 RX power increase from −30 dBm baseline
6. If RX power improves, update `media_settings.json` with the working values
7. Verify no regression on DAC ports (Ethernet16/32/48/112)

## Acceptance Criteria

- `media_settings.json` deployed to device directory
- No link regression on currently-working DAC ports (Ethernet16/32/48/112 remain up)
- xcvrd reads the file without errors (check `/var/log/syslog` for `xcvrd` warnings)
- After PW-04 physical fix, optical ports (27/28) achieve link-up with applied settings
- ASIC_DB `SAI_PORT_ATTR_SERDES_PREEMPHASIS` for port OIDs 27/28 reflects the JSON values

## Risks

- **EEPROM dependency**: `media_settings.json` is only applied when xcvrd correctly
  identifies the module type from EEPROM. If EEPROM reads remain corrupted (I2C contention),
  the file has no effect until the I2C daemon is deployed.
- **Wrong preemphasis for DAC ports**: If DAC port entries are added to the file with
  incorrect values, currently-working links will drop. Only touch ports 27 and 28.
- **SAI support**: Verify `SAI_PORT_ATTR_SERDES_PREEMPHASIS` is supported by the
  Broadcom SAI for Tomahawk (BCM56960). It is expected to be supported but needs
  confirmation that xcvrd actually programs it (check syslog after xcvrd restart).
- **Ordering relative to PW-04**: This file can be deployed before PW-04 physical fix
  is complete. It is preparatory work and causes no harm even if the optical ports
  remain physically broken.
