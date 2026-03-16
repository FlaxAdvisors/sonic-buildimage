# NF-05 — Speed Change: IMPLEMENTATION

## What Was Built

### Files Created

| File (repo-relative) | Description |
|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/platform.json` | Platform capabilities file |

### platform.json Structure

The `platform.json` file defines chassis SFPs and interface breakout modes. The speed
configuration for SONiC is implied by the breakout modes:

```json
{
    "chassis": {
        "name": "Wedge-100S-32X",
        "sfps": [ {"name": "sfp1"}, ... {"name": "sfp32"} ]
    },
    "interfaces": {
        "Ethernet0":   { "index": "1,1,1,1", "lanes": "117,118,119,120", ... },
        ...
    }
}
```

Each interface entry in the `interfaces` section specifies breakout modes. The supported
speed values derive from the mode key: `1x100G[40G]` implies 100G and 40G; `4x25G[10G]`
implies 25G and 10G per sub-port.

### Supported Speeds

From STATE_DB `supported_speeds` (populated by SAI at init):
- `40000` (40G)
- `100000` (100G)

25G is only available on sub-ports after breakout — not as a speed on 100G parent ports.

## Speed Change Behavior (verified on hardware 2026-03-02)

### Test: Ethernet0, 100G → 40G → 100G

**Baseline:**
```
Ethernet0  117,118,119,120  100G  9100  N/A  Ethernet1/1  routed  down  up  QSFP28 or later
```

**After `sudo config interface speed Ethernet0 40000`:**
- CONFIG_DB `PORT|Ethernet0` speed: `40000`
- APP_DB `PORT_TABLE:Ethernet0` speed: `40000`
- `show interfaces status Ethernet0`: Speed = `40G`
- Command accepted with rc=0, no errors

**BCM hardware state (unchanged):**
```
docker exec syncd bcmcmd "ps ce28"
  ce28(122)  !ena   4  100G  FD  SW  No  Forward  Untag  FA  KR4  9122
```

The `!ena` means admin-down (no peer link on Ethernet0) but hardware still reports `100G`.
This confirms the static BCM config locks serdes at 100G regardless of SAI speed config.

**After `sudo config interface speed Ethernet0 100000`:**
- CONFIG_DB: `100000`
- APP_DB: `100000`
- `show interfaces status`: `100G`
- No errors, no syncd crash

## Key Decision: Speed Change is Soft-Only

The SAI accepts the speed change and propagates it through the DB pipeline correctly.
However, the BCM hardware serdes is not dynamically reconfigured — the static BCM config
initialized all ports at 100G and they stay at 100G in hardware until syncd restarts.

This is acceptable behavior for the following reasons:
1. All current peer connections use 100G DAC — no need for actual 40G operation
2. True dynamic serdes reconfiguration would require either a BCM config template or
   a syncd restart workflow (config reload)
3. Other Accton Tomahawk platforms (AS7712) have the same behavior

For actual speed negotiation with a different-speed peer, a `config reload` would be needed
after the speed change to reinitialize syncd with the new port configuration.

## Hardware-Verified Facts

- verified on hardware 2026-03-02: `config interface speed Ethernet0 40000` — rc=0, no errors
- verified on hardware 2026-03-02: CONFIG_DB propagates speed=40000 correctly
- verified on hardware 2026-03-02: APP_DB propagates speed=40000 correctly
- verified on hardware 2026-03-02: `show interfaces status` shows `40G` after change
- verified on hardware 2026-03-02: BCM hardware remains at 100G (static serdes config)
- verified on hardware 2026-03-02: restore to 100G works, no side effects on other ports

## Remaining Known Gaps

- **40G link with actual peer not tested**: No 40G peer device available. Only DB-layer
  propagation has been verified; actual link-up at 40G is untested.
- **Speed change on 25G sub-ports**: After 4x25G breakout, changing a sub-port from 25G
  to 10G has not been tested. The BCM flex config includes `:50` on lane-2 sub-ports
  suggesting 50G support, but this has not been exercised.
- **platform.json interface speed validation**: The exact validation logic in sonic-cfggen
  for `config interface speed` against platform.json has not been traced. If sonic-cfggen
  rejects 40G on a port where it is not listed, the test would fail.
