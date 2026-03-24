# NF-06 — DPB (Dynamic Port Breakout): PLAN

## Problem Statement

The Wedge 100S-32X has 32 QSFP28 cages, each physically capable of 4×25G breakout
(the ASIC has 4 serdes lanes per cage). SONiC's DPB feature allows splitting a 100G
port into 4×25G sub-ports at runtime via `config interface breakout`.

Without DPB:
- The two QSFP→4x25G breakout cables (Ethernet64 and Ethernet80) cannot be used
- Server connections requiring 25G SFP28 over a breakout are blocked
- PortChannel1 cannot be tested at 25G member speeds

DPB requires three platform files:
1. `platform.json` — defines breakout modes and lane mappings per port
2. `hwsku.json` — default breakout mode per port (drives BREAKOUT_CFG initialization)
3. `th-wedge100s-32x-flex.config.bcm` — BCM config with sub-port records (`:i` flag)

And a system-level fix:
- `port_breakout_config_db.json` — default CONFIG_DB entries for sub-ports, must be in `/etc/sonic/`

## Proposed Approach

1. Create `platform.json` with three breakout modes per port: `1x100G[40G]`, `2x50G`, `4x25G[10G]`
2. Create `hwsku.json` with `default_brkout_mode: "1x100G[40G]"` for all 32 ports
3. Create flex BCM config (prerequisite NF-01) with sub-port records
4. Create `port_breakout_config_db.json` with default sub-port entries (admin_status=up, fec=none)
5. Fix `.deb` postinst to copy `port_breakout_config_db.json` to `/etc/sonic/` on install
6. Seed BREAKOUT_CFG table in CONFIG_DB on first-time DPB

## Files to Change

| File | Action |
|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/platform.json` | Create |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json` | Create |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_breakout_config_db.json` | Create |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/debian/sonic-platform-accton-wedge100s-32x.postinst` | Add copy + bash completion steps |
| `src/sonic-config-engine/portconfig.py` | Fix FEC condition; add admin_status=up |

## Acceptance Criteria

- `show interfaces breakout` lists all 32 ports with modes `1x100G[40G]`, `2x50G`, `4x25G[10G]`
- `config interface breakout Ethernet80 '4x25G[10G]' -y -f -l` completes without error
- Sub-ports Ethernet80/81/82/83 appear in `show interfaces status`
- Sub-ports show fec=none and admin=up
- At least one sub-port comes oper=up when breakout cable is connected
- Restore to `1x100G[40G]` works; Ethernet80 returns to single port

## Risks and Watch-Outs

- **Live DPB not supported on this SAI**: Initial testing showed orchagent SIGABRT on
  dynamic `create_port` with the fixed 100G config. The flex BCM config fixes this.
  If syncd was not started with the flex config, DPB will still crash.
- **port_breakout_config_db.json must be in /etc/sonic/**: The DPB CLI reads it from there,
  not from the hwsku directory. Postinst must copy it; a missing file causes
  "getDefaultConfig Failed" error and the breakout is silently rolled back.
- **Mode key must be exact**: The full string `"4x25G[10G]"` is required, not `4x25G` or
  `speed 25G`. Tab-completion requires Click 8.x format shell completion files.
- **BREAKOUT_CFG table must be seeded**: On first DPB attempt, `BREAKOUT_CFG` must exist
  in CONFIG_DB for every 100G parent port. Seed with `1x100G[40G]` for all 32 ports.
- **FEC on 25G sub-ports**: SAI rejects `rs` for 25G. Sub-ports must use `fec=none`.
  portconfig.py bug: the old FEC condition (`>= 50000 per lane`) set no FEC on 100G ports
  and no FEC on 25G ports. Fixed condition: `>= 40000` → rs; else → none.
- **Revert leaves ports admin-down**: A portconfig.py bug omits `admin_status=up` in the
  generated port dict. Fixed in the in-repo portconfig.py patch.
- **BCM interrupt flooding**: BCM56960 fires ~150 interrupts/sec causing SSH accept delays
  of 15–30s. Test SSH connections must use retry logic.
