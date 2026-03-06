# LACP + Breakout Config Session Notes

*Verified on hardware 2026-03-06 (hare-lorax, SONiC 6.1.0-29-2-amd64)*

## Work Done

### PortChannel1 (LACP) — Ethernet16 + Ethernet32 peered with rabbit-lorax

**Topology (confirmed via LLDP):**
- SONiC Ethernet32 (alias Ethernet9/1) ↔ rabbit-lorax Et14/1
- SONiC Ethernet16 (alias Ethernet5/1) ↔ rabbit-lorax Et13/1 (was admin down on SONiC)

**rabbit-lorax (`192.168.88.14`, Arista EOS on Wedge100S12V) pre-existing config:**
```
interface Ethernet13/1
   no switchport
   channel-group 1 mode active
interface Ethernet14/1
   no switchport
   channel-group 1 mode active
interface Port-Channel1
   no switchport
   ip address 10.0.1.0/31
```
Po1 was in state `D` (down) because SONiC had no LACP configured.

**SONiC steps taken:**
1. Removed IP `10.0.0.8/31` from Ethernet16 and `10.0.0.16/31` from Ethernet32 (required before adding to portchannel)
2. `sudo config portchannel add PortChannel1`
3. `sudo config portchannel member add PortChannel1 Ethernet32`
4. `sudo config interface startup Ethernet16`
5. `sudo config portchannel member add PortChannel1 Ethernet16`
6. `sudo config save -y`

**Result:**
```
PortChannel1  LACP(A)(Up)  Ethernet32(S) Ethernet16(S)
```
Both members: `aggregator ID: 7, Selected, state: current`
— "state: current" confirms LACP PDUs are being exchanged with the Arista peer.

**Note:** SONiC SSH not reachable from the build host via direct path to 192.168.88.14 (management VLAN/ACL restriction), but reachable from SONiC itself. This is a management network config issue unrelated to data plane.

---

### Dynamic Port Breakout — Ethernet64 and Ethernet80 to 4x25G

**Pre-requisite recovered:**
`/etc/sonic/port_breakout_config_db.json` was missing (lost between sessions — the CLI requires this in `/etc/sonic/` but the .deb only installs it to the hwsku dir).

Fix: `sudo cp /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_breakout_config_db.json /etc/sonic/`

**Commands:**
```bash
sudo config interface breakout Ethernet64 '4x25G[10G]' -y -f -l
sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l
sudo config save -y
```

**Results:**
| Port | Alias | FEC | Oper | Note |
|---|---|---|---|---|
| Ethernet64 | Ethernet17/1 | none | down | Transceiver not present (pre-existing physical issue) |
| Ethernet65 | Ethernet17/2 | none | down | No peer |
| Ethernet66 | Ethernet17/3 | none | down | No peer |
| Ethernet67 | Ethernet17/4 | none | down | No peer |
| Ethernet80 | Ethernet21/1 | none | **up** | 25G link established |
| Ethernet81 | Ethernet21/2 | none | **up** | 25G link established |
| Ethernet82 | Ethernet21/3 | none | down | No peer on this lane |
| Ethernet83 | Ethernet21/4 | none | down | No peer on this lane |

FEC was initially N/A (no field set). Fixed by explicit `config interface fec <port> none` for all
existing sub-ports + the two code fixes below (Bugs 4 and 5) so future breakouts are correct.

---

## Bugs Found

### 1. `port_breakout_config_db.json` not installed to `/etc/sonic/`

**Problem:** The DPB CLI (`config interface breakout`) reads default sub-port configuration from `/etc/sonic/port_breakout_config_db.json`. The file is in the hwsku dir at `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_breakout_config_db.json` but the .deb postinst doesn't copy it to `/etc/sonic/`.

**Symptom:**
```
getDefaultConfig Failed, Error: [Errno 2] No such file or directory: '/etc/sonic/port_breakout_config_db.json'
Port Addition Failed
[ERROR] Port breakout Failed!!! Opting Out
```

**Fix applied (postinst + live):**
Added to `debian/sonic-platform-accton-wedge100s-32x.postinst`:
```sh
HWSKU_DIR="/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X"
BREAKOUT_CFG="$HWSKU_DIR/port_breakout_config_db.json"
if [ -f "$BREAKOUT_CFG" ]; then
    cp "$BREAKOUT_CFG" /etc/sonic/port_breakout_config_db.json
    echo "wedge100s postinst: installed port_breakout_config_db.json to /etc/sonic/"
fi
```
Unconditional copy (not guarded by `[ ! -f ]`) so that `.deb` upgrades refresh the file
with the latest defaults (e.g., the `fec: none` fix in this session).
Live fix: `sudo cp` on hare-lorax at session start.

---

### 2. FEC not propagated to 100G ports via platform.json path (portconfig.py bug)

**Root cause — two separate issues:**

#### a) `port_config.ini` `fec` column is dead code when platform.json exists

When `platform.json` is present, `sonic-cfggen` calls `parse_platform_json_file()` in
`portconfig.py` instead of `parse_port_config_file()`. The platform.json path goes through
`BreakoutCfg.get_config()` and reads optional attributes from `hwsku.json`; it never
reads `port_config.ini` at all. So `fec rs` in `port_config.ini` has no effect.

The FEC=rs currently in CONFIG_DB for 100G parent ports was set during the initial
install (before platform.json existed) or carried over in the saved `config_db.json`.
A fresh `config reload --load-sysinfo` (initial boot default) would regenerate from
platform.json and 100G ports would lose their FEC setting.

#### b) `portconfig.py` FEC logic wrong for 100G/4-lane (upstream bug)

`portconfig.py:387`:
```python
# If the lane speed is greater than 50G, enable FEC
if entry.default_speed // lanes_per_port >= 50000:
    port_config['fec'] = 'rs'
```
For 1x100G[40G] with 4 lanes: `100000 // 4 = 25000 < 50000` → **no FEC set**.
The condition was written for PAM4 (50G per-lane NRZ) but 100GBASE-CR4 uses 25G NRZ
with RS-FEC (CL91). The per-lane speed check gives the wrong answer.

Correct condition: `entry.default_speed >= 40000` (applies to 40G, 50G, 100G ports).
For 4x25G sub-ports: `25000 < 40000` → no FEC (which is correct: SAI rejects `rs` for 25G).

**Hardware-verified FEC allowed values on this SAI:**
- 100G parent ports: `['none', 'rs']` — `fc` rejected
- 25G sub-ports: `['none', 'fc']` — `rs` rejected; `fc` brings link down on tested DAC cables; `none` correct for short DAC
- Phase-15 finding: `fc` rejected for 100G; `rs` rejected for 25G

**Fix applied (in-repo + running switch):**
Patched `src/sonic-config-engine/portconfig.py` `BreakoutCfg.get_config()`:
```python
# Before (wrong for 100G/4-lane):
# If the lane speed is greater than 50G, enable FEC
if entry.default_speed // lanes_per_port >= 50000:
    port_config['fec'] = 'rs'

# After:
# Set FEC based on port speed:
# - >= 40G (100G, 50G, 40G): RS-FEC (CL91) for CR4 links
# - < 40G (25G, 10G): explicit none (SAI rejects rs for 25G)
if entry.default_speed >= 40000:
    port_config['fec'] = 'rs'
else:
    port_config['fec'] = 'none'
```
Also patched live on hare-lorax: `/usr/local/lib/python3.13/dist-packages/portconfig.py`.

---

### 3. DPB CLI: tab-tab and speed-only syntax — HLD is aspirational, not implemented

**HLD (`doc/dynamic-port-breakout/sonic-dynamic-port-breakout-HLD.md`) shows:**
```
config interface breakout Ethernet0 speed 40G
config interface breakout Ethernet0 4x25G[10G]
```
The `speed 40G` form was a design intention — it was never implemented.

**Actual implementation (`src/sonic-utilities/config/main.py:5188`):**
```python
@click.argument('mode', required=True, type=click.STRING, shell_complete=_get_breakout_options)
def breakout(ctx, interface_name, mode, ...):
```
- `_get_breakout_options` reads `platform.json` and returns full mode strings (`"1x100G[40G]"`, `"2x50G"`, `"4x25G[10G]"`)
- Full mode string is **required** — `speed 40G` or bare `4x25G` are NOT accepted
- `shell_complete` provides tab-completion only if Click shell completion is activated in the shell (it is not enabled by default in SONiC's bash environment)

**Correct syntax (always use the full mode key from platform.json):**
```bash
sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l
sudo config interface breakout Ethernet80 '1x100G[40G]' -y -f -l
sudo config interface breakout Ethernet80 '2x50G' -y -f -l
```
No code change needed — this is an upstream documentation gap, not a defect in our port.

However, bash completion **was** broken (Bug 4 below) — users were seeing filesystem listing
instead of mode strings even when using the proper `_COMPLETE` mechanism.

---

### 4. Bash completion showing filesystem instead of breakout modes (Click 7 vs 8 API change)

**Symptom:** `config interface breakout Ethernet80 <tab><tab>` showed filesystem entries,
not `1x100G[40G]  2x50G  4x25G[10G]`.

**Root cause:** `/etc/bash_completion.d/config` (and other CLIs) were generated by Click 7.x
using `_CONFIG_COMPLETE=complete`. Click 8.x changed the env var to `_CONFIG_COMPLETE=bash_source`.
The stale completion files caused Click 8.x to silently fall back to readline's default
filesystem completion.

**Fix applied (live + postinst for future builds):**
```bash
# Regenerate on running switch (done manually):
_out=$(env "_CONFIG_COMPLETE=bash_source" config 2>/dev/null)
printf '%s\n' "$_out" > /etc/bash_completion.d/config
# ... same for show, sonic-clear, acl-loader, crm, pfcwd, pfc, counterpoll

# Permanent fix in postinst:
for _cli in config show sonic-clear acl-loader crm pfcwd pfc counterpoll; do
    _var="_$(echo "${_cli}" | tr 'a-z-' 'A-Z_')_COMPLETE"
    _out=$(env "${_var}=bash_source" "${_cli}" 2>/dev/null)
    if echo "${_out}" | grep -q '_completion()'; then
        printf '%s\n' "${_out}" > "/etc/bash_completion.d/${_cli}"
    fi
done
```
**Validated:** After regeneration, `COMPREPLY` correctly shows `1x100G[40G] 2x50G 4x25G[10G]`.
The postinst block is idempotent (checks for `_completion()` signature before overwriting).

---

### 5. DPB revert-to-1x100G leaves ports in admin down state

**Symptom:** After `config interface breakout Ethernet80 '1x100G[40G]' -y -f -l`, the
restored Ethernet80 had `admin_status = down` in CONFIG_DB, so the port would not come up.

**Root cause:** `BreakoutCfg.get_config()` in `portconfig.py` did not set `admin_status` in
the generated port dict. SONiC's default for an unset `admin_status` is `down`.

**Fix applied (in-repo + running switch):**
Added to the port_config dict in `portconfig.py:BreakoutCfg.get_config()`:
```python
port_config = {
    'admin_status': 'up',   # ← added
    'alias': ...,
    'lanes': ...,
    ...
}
```
Live patch applied to `/usr/local/lib/python3.13/dist-packages/portconfig.py` on hare-lorax.

---

### 6. 25G sub-port FEC showing N/A after breakout

**Symptom:** After 4x25G breakout, all sub-ports showed `N/A` in the FEC column of
`show interfaces status`.

**Root cause — two places:**
1. `portconfig.py` FEC block (Bug 2 above) had no `else` clause — 25G ports got no `fec` key.
2. `port_breakout_config_db.json` (read by DPB CLI for the initial CONFIG_DB write) also had
   no `fec` field on any of its 128 sub-port entries.

**Fix applied:**
- `portconfig.py`: added `else: port_config['fec'] = 'none'` (see Bug 2 fix above)
- `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/port_breakout_config_db.json`:
  all 128 entries updated with `"fec": "none"` (verified `"admin_status": "up"` already present)
- `/etc/sonic/port_breakout_config_db.json` on hare-lorax updated immediately via scp
- Existing broken-out ports fixed via: `config interface fec <port> none` for each N/A port
- `config save -y` to persist

**Hardware verified (2026-03-06):** All 25G sub-ports now show `none` in FEC column.

---

## Config Persistence Checklist

| Item | Persisted | How |
|---|---|---|
| PortChannel1 config | ✅ | `config_db.json` via `config save` |
| Breakout Ethernet64 / Ethernet80 | ✅ | `BREAKOUT_CFG` + `PORT` tables in `config_db.json` |
| FEC `none` on 25G sub-ports | ✅ | `PORT` table in `config_db.json` via `config save` |
| `/etc/sonic/port_breakout_config_db.json` | ✅ | Postinst now copies unconditionally on every `.deb` install |
| Bash completion (Click 8.x format) | ✅ | Postinst regenerates all 8 SONiC CLI completion files on every `.deb` install |
| Flex BCM config | ✅ | `sai.profile` points to `th-wedge100s-32x-flex.config.bcm` in hwsku dir (installed by .deb) |

## In-Repo Changes Required for Next `.bin` Build

| File | Change |
|---|---|
| `src/sonic-config-engine/portconfig.py` | Fix FEC condition (`>= 40000` not `// lanes >= 50000`); add `else: fec=none`; add `admin_status: up` |
| `device/accton/.../port_breakout_config_db.json` | All 128 sub-port entries: add `"fec": "none"` |
| `debian/sonic-platform-accton-wedge100s-32x.postinst` | Copy `port_breakout_config_db.json` to `/etc/sonic/`; regenerate Click 8.x bash completions |

All three files already updated in the repo on branch `wedge100s`.
