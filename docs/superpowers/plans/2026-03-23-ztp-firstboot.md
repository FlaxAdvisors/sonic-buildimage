# ZTP First-Boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the broken pre-generated `config_db.json` first-boot interception and replace it with standard SONiC ZTP, shipping L2/L3 reference ZTP profiles in the device directory.

**Architecture:** Three independent changes: (1) delete the static config and postinst block that intercepted first boot, (2) uncomment `ENABLE_ZTP=y` in `rules/config` so the ZTP package is bundled in the image, (3) create a `ztp/` reference directory with four JSON files operators can copy to their provisioning server.

**Tech Stack:** Shell (postinst), JSON (ZTP profiles and config templates), SONiC build system (`rules/config`).

---

## File Map

| Action | File |
|--------|------|
| Delete | `device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json` |
| Modify | `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` (remove lines 61–71) |
| Modify | `rules/config` (uncomment `ENABLE_ZTP = y`) |
| Create | `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l2-sample.json` |
| Create | `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l2-config_db.json` |
| Create | `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l3-sample.json` |
| Create | `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l3-config_db.json` |

---

## Task 1: Remove Static config_db.json

**Files:**
- Delete: `device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json`

- [ ] **Step 1: Delete the file**

```bash
rm device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json
```

- [ ] **Step 2: Verify it is gone**

```bash
ls device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json
```

Expected: `No such file or directory`

---

## Task 2: Remove First-Boot Config Interception from postinst

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` (lines 61–71)

The block to remove is exactly:

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

This is lines 61–71 in the current file. Line 60 ends with `fi` (closing the breakout cfg block) and line 72 is blank before the `usb0` comment.

- [ ] **Step 1: Remove the block**

Edit `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst`: delete lines 61–72 (the comment through the trailing blank line after `fi`).

The file currently reads at that range:
```
60: fi
61:
62: # Install factory default config_db.json to /etc/sonic/ only on first install
...
71: fi
72:
73: # Bring usb0 up immediately (CDC-ECM link to BMC).
```

After the edit lines 61–72 must be gone; line 60 (`fi`) should be immediately followed by a blank line then the `# Bring usb0 up immediately` comment.

- [ ] **Step 2: Syntax-check the result**

```bash
bash -n platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst
```

Expected: no output, exit 0.

- [ ] **Step 3: Verify the block is absent**

```bash
grep -n "FACTORY_CFG\|factory config_db" platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst
```

Expected: no output.

---

## Task 3: Enable ZTP in Build Config

**Files:**
- Modify: `rules/config`

The file currently has (around line 87):
```
# ENABLE_ZTP - installs Zero Touch Provisioning support.
# ENABLE_ZTP = y
```

- [ ] **Step 1: Uncomment ENABLE_ZTP**

Change `# ENABLE_ZTP = y` to `ENABLE_ZTP = y`.

- [ ] **Step 2: Verify the change**

```bash
grep "ENABLE_ZTP" rules/config
```

Expected output:
```
# ENABLE_ZTP - installs Zero Touch Provisioning support.
ENABLE_ZTP = y
```

---

## Task 4: Create ZTP Profile Directory and L2 Files

**Files:**
- Create: `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l2-sample.json`
- Create: `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l2-config_db.json`

- [ ] **Step 1: Create the directory**

```bash
mkdir -p device/accton/x86_64-accton_wedge100s_32x-r0/ztp
```

- [ ] **Step 2: Create ztp-l2-sample.json**

Write `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l2-sample.json`:

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

- [ ] **Step 3: Validate JSON**

```bash
python3 -m json.tool device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l2-sample.json > /dev/null
```

Expected: exit 0, no error output.

- [ ] **Step 4: Create l2-config_db.json**

Write `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l2-config_db.json`:

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

- [ ] **Step 5: Validate JSON**

```bash
python3 -m json.tool device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l2-config_db.json > /dev/null
```

Expected: exit 0, no error output.

---

## Task 5: Create L3 Placeholder Files

**Files:**
- Create: `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l3-sample.json`
- Create: `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l3-config_db.json`

- [ ] **Step 1: Create ztp-l3-sample.json**

Write `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l3-sample.json`:

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

- [ ] **Step 2: Validate JSON**

```bash
python3 -m json.tool device/accton/x86_64-accton_wedge100s_32x-r0/ztp/ztp-l3-sample.json > /dev/null
```

Expected: exit 0, no error output.

- [ ] **Step 3: Create l3-config_db.json**

Write `device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l3-config_db.json`:

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

**NOTE:** Do NOT add `INTERFACE`, `BGP_GLOBALS`, `BGP_NEIGHBOR`, or `PREFIX_SET` tables. This is an intentional placeholder — it must not be served via ZTP as-is without L3 customization.

- [ ] **Step 4: Validate JSON**

```bash
python3 -m json.tool device/accton/x86_64-accton_wedge100s_32x-r0/ztp/l3-config_db.json > /dev/null
```

Expected: exit 0, no error output.

---

## Task 6: Verify sonic-ztp Submodule and Final Check

- [ ] **Step 1: Confirm sonic-ztp submodule is initialized**

```bash
git submodule status src/sonic-ztp
```

Expected: a line starting with a space or `+` (not `-`) with the commit hash, e.g.:
```
 abc1234... src/sonic-ztp (heads/master)
```

If the line starts with `-`, the submodule is not initialized. Run:
```bash
git submodule update --init src/sonic-ztp
```

- [ ] **Step 2: Confirm all four ZTP files exist and are valid**

```bash
for f in ztp-l2-sample.json l2-config_db.json ztp-l3-sample.json l3-config_db.json; do
    python3 -m json.tool device/accton/x86_64-accton_wedge100s_32x-r0/ztp/$f > /dev/null && echo "OK: $f"
done
```

Expected:
```
OK: ztp-l2-sample.json
OK: l2-config_db.json
OK: ztp-l3-sample.json
OK: l3-config_db.json
```

- [ ] **Step 3: Confirm postinst has no FACTORY_CFG references**

```bash
grep -n "FACTORY_CFG\|factory config_db" \
    platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst
```

Expected: no output. (Note: `port_breakout_config_db.json` references in lines 49–59 are intentional and should remain.)

- [ ] **Step 4: Confirm ENABLE_ZTP is active**

```bash
grep "^ENABLE_ZTP" rules/config
```

Expected: `ENABLE_ZTP = y`

- [ ] **Step 5: Confirm config_db.json is deleted**

```bash
test ! -f device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json && echo "gone"
```

Expected: `gone`

- [ ] **Step 6: Confirm ztp/ directory is reachable by the sonic-device-data build**

The `sonic-device-data` package bundles the device directory via `cp -r -L ../../../device/*/* ./device/` in `src/sonic-device-data/Makefile` — no `.install` changes are needed. The `ztp/` subdirectory will be included automatically. Verify it is in the right location:

```bash
ls device/accton/x86_64-accton_wedge100s_32x-r0/ztp/
```

Expected: `ztp-l2-sample.json  l2-config_db.json  ztp-l3-sample.json  l3-config_db.json`
