# NF-06 — DPB (Dynamic Port Breakout): IMPLEMENTATION

## What Was Built

### Files Created/Modified

| File (repo-relative) | Action |
|---|---|
| `device/accton/x86_64-accton_wedge100s_32x-r0/platform.json` | Created |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json` | Created |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm` | Created (NF-01) |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile` | Created (NF-01) |
| `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_breakout_config_db.json` | Created; all 128 sub-port entries with `"fec": "none"`, `"admin_status": "up"` |
| `platform/.../debian/sonic-platform-accton-wedge100s-32x.postinst` | Added port_breakout_config_db.json copy; bash completion regeneration |
| `src/sonic-config-engine/portconfig.py` | Fixed FEC condition and admin_status |

### platform.json — Breakout Modes

Three modes per port:
- `"1x100G[40G]"` — default, single 100G (or 40G) port
- `"2x50G"` — two 50G sub-ports
- `"4x25G[10G]"` — four 25G (or 10G) sub-ports

Lane mappings match `port_config.ini` exactly (1-based, 1–128).
Reference model: Wedge100BF-32QS platform.json (same port count, same vendor).

**Why 3 modes**: Stage 14 tests use `EXPECTED_BREAKOUT_MODES = {"1x100G[40G]", "2x50G", "4x25G[10G]"}`.
An earlier version had 4 modes (`"4x25G"` and `"4x10G"` separately) which was wrong.

### hwsku.json — Default Breakout Mode

All 32 ports: `"default_brkout_mode": "1x100G[40G]"`. No autoneg or FEC defaults.

### postinst — Key Additions

```sh
# Copy DPB config to /etc/sonic/ (required by DPB CLI)
HWSKU_DIR="/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X"
cp "$HWSKU_DIR/port_breakout_config_db.json" /etc/sonic/port_breakout_config_db.json

# Regenerate Click 8.x bash completions
for _cli in config show sonic-clear acl-loader crm pfcwd pfc counterpoll; do
    _var="_$(echo "${_cli}" | tr 'a-z-' 'A-Z_')_COMPLETE"
    _out=$(env "${_var}=bash_source" "${_cli}" 2>/dev/null)
    if echo "${_out}" | grep -q '_completion()'; then
        printf '%s\n' "${_out}" > "/etc/bash_completion.d/${_cli}"
    fi
done
```

Copy is unconditional (refreshes on every `.deb` upgrade, picks up `fec: none` fix).

### portconfig.py Patches

**Bug 1 — FEC condition wrong for 100G/4-lane:**
```python
# Before (wrong: 100000 // 4 = 25000 < 50000 → no FEC):
if entry.default_speed // lanes_per_port >= 50000:
    port_config['fec'] = 'rs'

# After:
if entry.default_speed >= 40000:
    port_config['fec'] = 'rs'
else:
    port_config['fec'] = 'none'
```

**Bug 2 — admin_status omitted from generated dict:**
```python
port_config = {
    'admin_status': 'up',   # added
    'alias': ...,
    'lanes': ...,
    ...
}
```

**Path**: `/usr/local/lib/python3.13/dist-packages/portconfig.py` (live) and
`src/sonic-config-engine/portconfig.py` (in-repo).

## Runtime Path Discovery

SONiC resolves files via:
- `platform.json`: `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/platform.json`
- `hwsku.json`: `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json`

Note: `/usr/share/sonic/platform/` symlink does not exist on this platform. Tests must
use the `device/` path directly.

## DPB Workflow That Works

```bash
# Ensure port_breakout_config_db.json is in /etc/sonic/
sudo cp /usr/share/sonic/device/.../Accton-WEDGE100S-32X/port_breakout_config_db.json /etc/sonic/

# Break out (live — works with flex BCM config + patched portconfig.py)
sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l

# Revert
sudo config interface breakout Ethernet80 '1x100G[40G]' -y -f -l
sudo config save -y
```

## Hardware-Verified Facts

- verified on hardware 2026-03-03: all 100G ports work after switching to flex BCM config
- verified on hardware 2026-03-03: live DPB works on Ethernet64 and Ethernet80 without reboot
- verified on hardware 2026-03-03: Ethernet80/81 show oper=up with 25G breakout cable connected
- verified on hardware 2026-03-06: 18/18 stage 14 tests pass with pmon running
- verified on hardware 2026-03-06: FEC=none on all 25G sub-ports after portconfig.py fix
- verified on hardware 2026-03-06: `show interfaces breakout` shows all 3 modes for all 32 ports
- verified on hardware 2026-03-06: revert to 1x100G[40G] leaves port admin=up (after portconfig.py fix)

## Remaining Known Gaps

- **2x50G mode not tested**: platform.json includes it but no 2x50G breakout cable is available.
- **Ethernet64 transceiver absent**: Port 17 QSFP physical seating issue; sub-ports exist
  but show oper=down due to absent transceiver.
- **BREAKOUT_CFG manual seeding required for fresh install**: On initial install,
  `BREAKOUT_CFG` table is not populated. First use of `config interface breakout` requires
  the table to exist. The stage 14 tests seed it if missing.
