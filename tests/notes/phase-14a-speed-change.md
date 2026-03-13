# Phase 14a: Speed Change Verification

*Tested 2026-03-02 on hare-lorax (Wedge 100S-32X, SONiC)*

## Test: 100G → 40G → 100G on Ethernet0

Ethernet0 was chosen because it has no active peer link (breakout cable, no connected devices).

### Baseline
```
  Interface            Lanes    Speed    MTU    FEC        Alias    Vlan    Oper    Admin             Type
  Ethernet0  117,118,119,120     100G   9100    N/A  Ethernet1/1  routed    down       up  QSFP28 or later
```

### Speed change to 40G
```
sudo config interface speed Ethernet0 40000
```
- Command accepted with no errors (verified on hardware 2026-03-02)

### DB propagation
- CONFIG_DB (`redis-cli -n 4 hget "PORT|Ethernet0" speed`): **40000** (verified on hardware 2026-03-02)
- APP_DB (`redis-cli -n 0 hget "PORT_TABLE:Ethernet0" speed`): **40000** (verified on hardware 2026-03-02)
- `show interfaces status Ethernet0`: Speed column shows **40G** (verified on hardware 2026-03-02)

### BCM ASIC state
```
docker exec syncd bcmcmd "ps ce28"
      ce28(122)  !ena   4  100G  FD   SW  No   Forward         Untag   FA    KR4  9122    No
```
- BCM port ce28 shows `!ena` (disabled, no peer link) and still reports 100G
- This is expected: the static BCM config (`th-wedge100s-32x100G.config.bcm`) initializes all ports at 100G
- SAI accepted the speed change at the config/app layer but BCM serdes was not dynamically reconfigured
- Comparison: linked port ce0 (Ethernet16) shows `up 4 100G` — confirming `!ena` is due to no peer, not the speed change

### Revert to 100G
```
sudo config interface speed Ethernet0 100000
```
- Command accepted with no errors
- CONFIG_DB: **100000** (verified on hardware 2026-03-02)
- APP_DB: **100000** (verified on hardware 2026-03-02)
- `show interfaces status`: **100G** (verified on hardware 2026-03-02)

## Key Findings

1. **SAI accepts speed changes** — `config interface speed` works for 100G→40G and back without errors
2. **DB pipeline propagates correctly** — CONFIG_DB → APP_DB → CLI all reflect the new speed
3. **BCM hardware does not dynamically reconfigure** — the static `.config.bcm` file locks serdes at 100G. Actual hardware speed change likely requires syncd restart (or a BCM config that supports the target speed)
4. **No impact on other ports** — changing Ethernet0's speed did not affect the 4 linked ports

## Implications for DPB (Phase 14b)

- Speed changes are accepted at the SAI/config layer, which is the prerequisite for DPB
- Actual breakout (4x25G, 2x50G) will likely require `config reload` to reinitialize syncd with new port layout, since the Tomahawk BCM config is static
- True dynamic port breakout (no reload) would require a Jinja2 BCM config template — this is a future enhancement
