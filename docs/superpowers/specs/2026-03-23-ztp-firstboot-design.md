# ZTP First-Boot Design: Remove Pre-Generated Config, Enable ZTP

**Date:** 2026-03-23
**Branch:** wedge100s
**Status:** Approved

## Background

The L2 completion plan (2026-03-22) introduced a static `config_db.json` shipped via the platform `.deb` and installed on first boot via `postinst`. The intent was to disable BGP and enable a management VRF. This approach failed in practice — the postinst guard condition (`[ ! -f /etc/sonic/config_db.json ]`) may not reliably fire during all install paths, and the approach is presumptuous for SONiC-familiar operators who expect standard first-boot behavior. The correct enterprise mechanism is ZTP.

## Goals

1. Remove the platform-specific first-boot config interception.
2. Enable SONiC ZTP in the build so the standard ZTP code path runs on first boot.
3. Ship two reference ZTP profiles (L2 and L3 placeholder) in the device directory.

## Out of Scope

- ZTP provisioning server setup (operator concern).
- Full L3 profile (BGP neighbors, prefix-lists, route-maps) — deferred to a future L3 brainstorm.
- Test stage for ZTP (no lab DHCP/ZTP server available for automated testing).

---

## Section 1: Cleanup

### Files Modified
- `device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json` — **deleted**
- `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` — remove lines 61–71

### Postinst Block Removed
```bash
# Install factory default config_db.json to /etc/sonic/ only on first install
# (i.e. when /etc/sonic/config_db.json does not yet exist).  This pre-empts
# the sonic-cfggen T0 topology generation that would otherwise flood every
# port with BGP neighbors and ARP traffic on first boot.
DEVICE_DIR="/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
FACTORY_CFG="$DEVICE_DIR/config_db.json"
TARGET_CFG="/etc/sonic/config_db.json"
if [ -f "$FACTORY_CFG" ] && [ ! -f "$TARGET_CFG" ]; then
    cp "$FACTORY_CFG" "$TARGET_CFG"
    echo "wedge100s postinst: installed factory config_db.json to /etc/sonic/"
fi
```

### Resulting First-Boot Behavior

With no `config_db.json` present at first boot, `config-setup` follows the standard SONiC path:
1. `minigraph.xml` present → use minigraph (unchanged)
2. ZTP enabled (`/usr/bin/ztp` exists) → initiate ZTP ← **new default**
3. ZTP disabled → `generate_config factory` via sonic-cfggen

**Note on `ztp_is_enabled()` and service ordering:** `ztp.service` is ordered `After=config-setup.service`, so it has not yet started when `config-setup` calls `ztp_is_enabled()`. This is not a problem: `ztp status -c` does not require the service — it reads `admin-mode` from `/host/ztp/ztp_cfg.json`, falling back to the in-memory default of `True` (see `src/sonic-ztp/.../defaults.py` line 21) when the file is absent. On a fresh install with no ZTP config file, `ztp status -c` returns the enabled exit code, and `ztp_is_enabled()` correctly returns true.

---

## Section 2: Build Enablement

### File Modified
- `rules/config`

### Change
Uncomment or add:
```
ENABLE_ZTP = y
```

### Effect
`sonic-ztp_1.0.0_all.deb` is included in the broadcom image assembly (`slave.mk` line 1502 already conditioned on `ENABLE_ZTP`). This delivers:
- `/usr/bin/ztp` — ZTP CLI
- `/usr/lib/ztp/` — ZTP engine, plugins, DHCP hook
- `/usr/lib/ztp/plugins/configdb-json` — applies downloaded config_db.json
- `/etc/dhcp/dhclient-exit-hooks.d/ztp` — triggers ZTP on DHCP lease acquisition

No other build changes are required. The `config-setup` script already detects `/usr/bin/ztp` via `ztp_is_enabled()` and routes to the ZTP code path.

---

## Section 3: ZTP Profile Directory

### New Directory
`device/accton/x86_64-accton_wedge100s_32x-r0/ztp/`

Installed to `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/ztp/` on the switch. Operators can read the samples directly on a deployed switch and copy them to their provisioning server.

### Files Created

| File | Purpose |
|------|---------|
| `ztp-l2-sample.json` | ZTP profile for L2 deployment (serve via DHCP option 67) |
| `l2-config_db.json` | Config template the L2 profile fetches and applies |
| `ztp-l3-sample.json` | ZTP profile placeholder for L3 deployment |
| `l3-config_db.json` | Config template placeholder for L3 deployment |

---

## Section 4: L2 Profile Content

### `ztp-l2-sample.json`

Single-section ZTP profile using the `configdb-json` plugin with a `dynamic-url`. The switch constructs the config URL from its own hostname, allowing per-switch customization from a shared provisioning server.

```json
{
    "ztp": {
        "01-configdb-json": {
            "dynamic-url": {
                "source": {
                    "prefix": "http://192.0.2.1/ztp/",
                    "identifier": "hostname",
                    "suffix": "_config_db.json"
                }
            }
        }
    }
}
```

**Operator instructions (as comments in a companion README or inline doc):**
- Replace `192.0.2.1` with the provisioning server IP/hostname.
- Serve this file at the URL returned by DHCP option 67.
- For each switch, serve `<hostname>_config_db.json` (e.g. `spine-01_config_db.json`) based on `l2-config_db.json`.

### `l2-config_db.json`

Template config applied by the L2 ZTP profile. Operator customizes per-switch (hostname, mgmt IP, gateway) before serving.

**Contents:**

```json
{
    "DEVICE_METADATA": {
        "localhost": {
            "hostname": "REPLACE-WITH-HOSTNAME",
            "platform": "x86_64-accton_wedge100s_32x-r0",
            "hwsku": "Accton-WEDGE100S-32X",
            "mac": "00:00:00:00:00:00",
            "type": "LeafRouter"
        }
    },
    "LOOPBACK_INTERFACE": {
        "Loopback0": {}
    },
    "MGMT_VRF_CONFIG": {
        "vrf_global": {
            "mgmtVrfEnabled": "true"
        }
    },
    "MGMT_INTERFACE": {
        "eth0": {},
        "eth0|REPLACE-WITH-IP/PREFIX": {
            "gwaddr": "REPLACE-WITH-GATEWAY"
        }
    },
    "FEATURE": {
        "bgp": {
            "state": "disabled",
            "auto_restart": "disabled",
            "has_per_asic_scope": "False",
            "has_global_scope": "True",
            "has_timer": "False"
        },
        "spanning_tree": {
            "state": "enabled",
            "auto_restart": "enabled",
            "has_per_asic_scope": "False",
            "has_global_scope": "True",
            "has_timer": "False"
        }
    },
    "PORT": {
        "Ethernet0":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet4":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet8":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet12":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet16":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet20":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet24":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet28":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet32":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet36":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet40":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet44":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet48":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet52":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet56":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet60":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet64":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet68":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet72":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet76":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet80":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet84":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet88":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet92":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet96":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet100": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet104": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet108": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet112": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet116": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet120": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet124": {"speed": "100000", "fec": "rs", "admin_status": "up"}
    }
}
```

**MGMT_INTERFACE note:** The `"eth0": {}` entry creates the interface object in CONFIG_DB. The `"eth0|IP/PREFIX"` entry binds the static IP. When mgmt VRF is enabled, eth0 is automatically placed in the `mgmt` VRF by SONiC — no explicit VRF binding key is needed in this table.

---

## Section 5: L3 Profile Content (Placeholder)

### `ztp-l3-sample.json`

```json
{
    "ztp": {
        "01-configdb-json": {
            "dynamic-url": {
                "source": {
                    "prefix": "http://192.0.2.1/ztp/",
                    "identifier": "hostname",
                    "suffix": "_l3_config_db.json"
                }
            }
        }
    }
}
```

### `l3-config_db.json`

Minimal skeleton. Fields marked `REPLACE` require per-switch customization. Deferred tables are listed after the JSON block — do not add them until the L3 brainstorm.

```json
{
    "DEVICE_METADATA": {
        "localhost": {
            "hostname": "REPLACE-WITH-HOSTNAME",
            "platform": "x86_64-accton_wedge100s_32x-r0",
            "hwsku": "Accton-WEDGE100S-32X",
            "mac": "00:00:00:00:00:00",
            "type": "LeafRouter"
        }
    },
    "FEATURE": {
        "bgp": {
            "state": "enabled",
            "auto_restart": "enabled",
            "has_per_asic_scope": "False",
            "has_global_scope": "True",
            "has_timer": "False"
        }
    },
    "PORT": {
        "Ethernet0":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet4":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet8":   {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet12":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet16":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet20":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet24":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet28":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet32":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet36":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet40":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet44":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet48":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet52":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet56":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet60":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet64":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet68":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet72":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet76":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet80":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet84":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet88":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet92":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet96":  {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet100": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet104": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet108": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet112": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet116": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet120": {"speed": "100000", "fec": "rs", "admin_status": "up"},
        "Ethernet124": {"speed": "100000", "fec": "rs", "admin_status": "up"}
    },
    "LOOPBACK_INTERFACE": {
        "Loopback0": {}
    }
}
```

**Deferred tables (not in the placeholder file — add before serving):**
- `INTERFACE` — per-port IP addresses (e.g. `"Ethernet0|10.0.0.1/31": {}`)
- `BGP_GLOBALS` — ASN, router-id
- `BGP_NEIGHBOR` — peer IPs and ASNs
- `PREFIX_SET` / `ROUTE_MAP` — policy

These are omitted intentionally. The `l3-config_db.json` placeholder must not be served via ZTP as-is — it will not produce a functional L3 switch. Full L3 config is deferred to a dedicated L3 brainstorm.

---

## Implementation Checklist

- [ ] Delete `device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json`
- [ ] Remove lines 61–71 from `postinst`
- [ ] Verify `bash -n postinst` passes
- [ ] Create `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l2-sample.json`
- [ ] Verify: `python3 -m json.tool device/.../ztp/ztp-l2-sample.json`
- [ ] Create `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l2-config_db.json`
- [ ] Verify: `python3 -m json.tool device/.../ztp/l2-config_db.json`
- [ ] Create `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l3-sample.json`
- [ ] Verify: `python3 -m json.tool device/.../ztp/ztp-l3-sample.json`
- [ ] Create `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l3-config_db.json`
- [ ] Verify: `python3 -m json.tool device/.../ztp/l3-config_db.json`
- [ ] Set `ENABLE_ZTP=y` in `rules/config`
- [ ] Confirm `src/sonic-ztp` submodule is initialized (`git submodule status src/sonic-ztp`)
