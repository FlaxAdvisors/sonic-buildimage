# Phase 14b: Dynamic Port Breakout (DPB) — platform.json & hwsku.json

*Tested 2026-03-02/03 on hare-lorax (Wedge 100S-32X, SONiC)*

## Files Created

### platform.json
- **Location (repo)**: `device/accton/x86_64-accton_wedge100s_32x-r0/platform.json`
- **Location (switch runtime)**: `/usr/share/sonic/platform/platform.json`
- **Structure**: chassis section (32 SFPs) + interfaces section (32 parent ports)
- **Breakout modes per port**: `1x100G[40G]`, `2x50G`, `4x25G[10G]`
- **Lane mappings**: Match port_config.ini exactly (1-based, 1-128)
- **Reference model**: Wedge100BF-32QS platform.json (same port count, same vendor)

### hwsku.json
- **Location (repo)**: `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json`
- **Location (switch runtime)**: `/usr/share/sonic/platform/Accton-WEDGE100S-32X/hwsku.json`
- **Structure**: 32 ports, all `default_brkout_mode: "1x100G[40G]"`
- **No autoneg/fec defaults** (those are peer-dependent)

## Deployment Notes

### Path resolution
- SONiC resolves platform dir via `sonic_py_common.device_info.get_path_to_platform_dir()` → `/usr/share/sonic/platform/`
- HWSKU dir: `/usr/share/sonic/platform/Accton-WEDGE100S-32X/`
- Note: This is NOT the same as `/usr/share/sonic/device/...` where the build installs files
- The `device/` path is for build-time; `platform/` is the runtime symlink

### BREAKOUT_CFG initialization
- First-time DPB on an existing installation requires seeding `BREAKOUT_CFG` table in CONFIG_DB
- Without it: `[ERROR] BREAKOUT_CFG table is NOT present in CONFIG DB`
- Initialized with: `db.set_entry("BREAKOUT_CFG", "EthernetN", {"brkout_mode": "1x100G[40G]"})` for all 32 ports (verified on hardware 2026-03-02)

## Test Results

### `show interfaces breakout` — SUCCESS (verified on hardware 2026-03-02)
- All 32 ports listed with their breakout modes
- Correct lanes, indices, and alias names displayed

### `config interface breakout Ethernet64 4x25G[10G] -y` — PARTIAL SUCCESS
- CLI output showed correct port deletion and creation plan:
  ```
  Ports to be deleted: {"Ethernet64": "100000"}
  Ports to be added: {"Ethernet64": "25000", "Ethernet65": "25000", "Ethernet66": "25000", "Ethernet67": "25000"}
  Breakout process got successfully completed.
  ```
- CONFIG_DB correctly updated: 4 child ports with correct lanes (53, 54, 55, 56) and aliases (Ethernet17/1-4) (verified on hardware 2026-03-02)
- **BUT**: orchagent crashed (SIGABRT) when trying to create new ports at the SAI level
- Only Ethernet64 appeared in APP_DB; Ethernet65-67 did not
- swss and syncd containers went down

### Root cause: SAI does not support dynamic port creation
- The Broadcom SAI on this Tomahawk build does not implement `create_port` / `remove_port` SAI APIs
- orchagent SIGABRT when attempting to create new port objects
- This is a known limitation of older Broadcom SAI versions on Tomahawk

### Recovery
1. Reverted CONFIG_DB: deleted Ethernet65-67, restored Ethernet64 to 4-lane 100G
2. `config save` to persist
3. `systemctl restart swss` to bring back swss/syncd
4. All 4 linked ports (Ethernet16, 32, 48, 112) came back up with RS-FEC (verified on hardware 2026-03-03)

## Key Findings

1. **platform.json and hwsku.json are correct** — SONiC loads them, `show interfaces breakout` works perfectly
2. **BREAKOUT_CFG table must be seeded** for first-time DPB on existing installations
3. **Live DPB is NOT supported** on this SAI — orchagent crashes on dynamic port creation
4. **Breakout requires `config reload`** — the alternative workflow:
   - Modify CONFIG_DB with desired breakout ports (via CLI or scripted)
   - `config save`
   - `config reload` (restarts syncd with new port layout from scratch)
   - This should work because syncd re-reads CONFIG_DB and creates all ports at init time
5. **The files themselves are production-ready** — the issue is SAI, not our configuration

## Next Steps

- [ ] Test breakout via `config reload` path (not live DPB)
- [ ] Test breakout on Ethernet64 (Port 17, has 4x25G breakout cable to compute nodes)
- [ ] Test breakout on Ethernet80 (Port 21, also has breakout cable)
- [ ] Consider BCM config changes for non-100G port initialization
