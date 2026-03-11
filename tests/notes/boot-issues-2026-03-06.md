# Boot Issues — Fresh Install 2026-03-06

## Issue 1: postinst exits with code 1 (dpkg Half-Configured)

### Symptom
On first boot after `sonic-broadcom.bin` install, dpkg logs:
```
installed sonic-platform-accton-wedge100s-32x package post-installation script subprocess returned error exit status 1
```
Last visible postinst message: `wedge100s postinst: refreshed bash completion for config`

### Root Cause
`/bin/sh` on Debian Trixie is `dash`, which (unlike bash) propagates `set -e` through
variable assignments where the RHS command substitution exits non-zero.
In the bash completion loop:
```sh
_out=$(env "${_var}=bash_source" "${_cli}" 2>/dev/null)
```
The `show` CLI (second loop iteration) exits non-zero because Redis is not yet running
during rc.local first-boot, causing dash's `set -e` to abort the postinst.

### Fix
Added `|| true` inside the command substitution so the subshell always exits 0:
```sh
_out=$(env "${_var}=bash_source" "${_cli}" 2>/dev/null || true)
```
Also added explicit `exit 0` at end of postinst.

### Files Changed
- `platform/broadcom/sonic-platform-modules-accton/debian/sonic-platform-accton-wedge100s-32x.postinst` line 68

### Live Fix (target already running)
```bash
# Patch the installed postinst
sudo python3 -c "
path = '/var/lib/dpkg/info/sonic-platform-accton-wedge100s-32x.postinst'
with open(path) as f: text = f.read()
old = '_out=\$(env \"\${_var}=bash_source\" \"\${_cli}\" 2>/dev/null)'
new = '_out=\$(env \"\${_var}=bash_source\" \"\${_cli}\" 2>/dev/null || true)'
with open(path,'w') as f: f.write(text.replace(old, new, 1))
print('patched')
"
# Clear the Half-Configured state
sudo dpkg --configure sonic-platform-accton-wedge100s-32x
```

---

## Issue 2: syncd crashes (exit status 2) → swss/bgp fail → no interfaces

### Symptom
```
● swss.service  loaded failed failed  switch state service
● bgp.service   loaded failed failed  BGP container
show interfaces status  → (empty table)
```
Syslog shows:
```
ERR syncd#syncd: [none] SAI_API_SWITCH:platform_config_file_set:116 Invalid YAML configuration file: /usr/share/sonic/hwsku/th-wedge100s-32-flex.config.bcm rv: -1
```

### Root Cause
`sai.profile` referenced `th-wedge100s-32-flex.config.bcm` (missing the `x`).
The actual filename is `th-wedge100s-32x-flex.config.bcm`.
BCM SAI cannot open the file → logs "Invalid YAML configuration file" → syncd exits 2 →
swss restart-loops → hits systemd start-limit-hit → bgp/radv/teamd all cascade-fail.

### Fix
Corrected the filename in `sai.profile`:
```
SAI_INIT_CONFIG_FILE=/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm
```

### Files Changed
- `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile` line 1

### Live Fix (target already running)
```bash
# Fix the deployed sai.profile
sudo sed -i "s/th-wedge100s-32-flex/th-wedge100s-32x-flex/" \
  /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile

# Reset start-limit-hit and restart the whole swss stack
sudo systemctl reset-failed swss syncd bgp radv teamd
sudo systemctl start swss
```
After ~15 seconds all containers come up and `show interfaces status` shows all 32 ports.

---

## Other Console Messages (non-critical)

- `error: no suitable video mode found. Booting in blind mode` — GRUB EFI/VGA cosmetic; no impact.
- `Temporary failure resolving 'download.docker.com'` — expected on isolated network; apt-get ignores and continues.
- `kdump-tools: no crashkernel= parameter` — kdump not functional; expected for this platform.
