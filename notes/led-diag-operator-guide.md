# Wedge 100S-32X LED Diagnostic Tooling — Operator Guide

This guide is for a person standing in front of the switch with SSH access.
All commands run on the SONiC host as root (`sudo`).

## Tools Overview

There are **two** LED diagnostic tools and one supporting daemon:

| Tool | Path on target | What it does |
|------|---------------|--------------|
| `wedge100s-led-diag-bmc.py` | `/usr/local/bin/` | CPLD LED control via bmc-daemon (safe, daemon-mediated) |
| `wedge100s-led-diag.py` | `/usr/local/bin/` | Direct ASIC + CPLD access via /dev/mem and BMC SSH (advanced) |
| `wedge100s-bmc-daemon` | `/usr/bin/` | Background service that relays commands to BMC via SSH |

**For routine visual verification, use `wedge100s-led-diag-bmc.py`.** It goes through
the bmc-daemon's safe serialized path and cannot corrupt the I2C bus.

## Prerequisites

Before running anything, confirm the bmc-daemon is up:

```bash
sudo systemctl status wedge100s-bmc-daemon
```

If it's not running:
```bash
sudo systemctl start wedge100s-bmc-daemon
```

## Quick Health Check

```bash
sudo wedge100s-led-diag-bmc.py status
```

This reads CPLD registers 0x3c and 0x3d via the daemon and reports the current
LED mode. Expected output (normal operation):

```
=== CPLD LED Control (0x3c) via bmc-daemon ===
  raw value:      0x02
  test_mode_en:   False
  test_blink_en:  False
  th_led_steam:   0
  walk_test_en:   False
  th_led_en:      True
  th_led_clear:   False

Mode: PASSTHROUGH (Tomahawk controls LEDs)
```

**Passthrough (0x02)** is the normal operating mode — the Tomahawk ASIC's LEDUP
processors drive the front-panel LEDs based on link/activity status.

## Sysfs Files in /run/wedge100s/

These files are the daemon's output. Read them anytime to see current state:

| File | Content | Updated by |
|------|---------|-----------|
| `cpld_led_ctrl` | Integer value of CPLD register 0x3c | Any LED write or `cpld_led_ctrl.set` trigger |
| `cpld_led_color` | Integer value of CPLD register 0x3d (test color) | `led_color_read.set` trigger |
| `led_diag_results.json` | Structured results from last `demo` run | `wedge100s-led-diag-bmc.py demo` |

Read them directly:
```bash
cat /run/wedge100s/cpld_led_ctrl       # e.g. "2" means 0x02 = passthrough
cat /run/wedge100s/cpld_led_color      # test color register
cat /run/wedge100s/led_diag_results.json | python3 -m json.tool
```

## Manual Visual Verification (Step-by-Step)

The `demo` command runs all patterns automatically with 3-second pauses — it does
**not** wait for user input between stages. For visual confirmation with time to
inspect each pattern, run each step manually:

### Step 1: Baseline — Read Current State

```bash
sudo wedge100s-led-diag-bmc.py status
```

**Look at the front panel.** Note current LED state before changing anything.

### Step 2: All Off

```bash
sudo wedge100s-led-diag-bmc.py set off
```

**Sysfs confirms:** `cat /run/wedge100s/cpld_led_ctrl` → `0`

**At the front panel:** All 32 port LEDs should be **completely dark** (no light at all).
If any LEDs remain lit, the CPLD may not be responding to writes — see Troubleshooting.

### Step 3: Solid Colors (steam 0-3)

Run each, pausing to inspect:

```bash
sudo wedge100s-led-diag-bmc.py set solid 0
```
**Sysfs:** `cpld_led_ctrl` → `128` (0x80). **Front panel:** All 32 ports show **same solid color** (color depends on CPLD color table, typically green).

```bash
sudo wedge100s-led-diag-bmc.py set solid 1
```
**Sysfs:** `cpld_led_ctrl` → `144` (0x90). **Front panel:** All 32 ports solid, **different color** from steam=0.

```bash
sudo wedge100s-led-diag-bmc.py set solid 2
```
**Sysfs:** `cpld_led_ctrl` → `160` (0xA0). **Front panel:** Another solid color.

```bash
sudo wedge100s-led-diag-bmc.py set solid 3
```
**Sysfs:** `cpld_led_ctrl` → `176` (0xB0). **Front panel:** Fourth solid color.

**What you're confirming:** Each steam value produces a *different* uniform color
across all 32 ports. If all four look identical, the CPLD color register (0x3d)
may be stuck. Check: `sudo wedge100s-led-diag-bmc.py status` after each.

### Step 4: Rainbow

```bash
sudo wedge100s-led-diag-bmc.py set rainbow
```

**Sysfs:** `cpld_led_ctrl` → `224` (0xE0). **Front panel:** All 32 ports **cycling through
colors in a blinking/scrolling rainbow pattern**. This is the CPLD's built-in test
pattern — it's the same "all magenta" pattern you see at boot, but the blink bit
makes it cycle.

### Step 5: Walk

```bash
sudo wedge100s-led-diag-bmc.py set walk
```

**Sysfs:** `cpld_led_ctrl` → `8` (0x08). **Front panel:** A single lit LED **walks across
the 32 ports** sequentially (port 1 lights, then port 2, etc.). This tests that
the CPLD's scan chain addressing works for individual ports.

### Step 6: Passthrough (Return to Normal)

```bash
sudo wedge100s-led-diag-bmc.py set passthrough
```

**Sysfs:** `cpld_led_ctrl` → `2` (0x02). **Front panel:** LEDs now reflect **actual link
status** from the Tomahawk ASIC. Ports with cables and established links should
show green. Ports with no cable should be dark.

**IMPORTANT:** Always end with passthrough to return to normal operation.

**Known issue:** In passthrough mode, if the LEDUP processors are not running
(LEDUP_EN=0, which happens after syncd init), all LEDs will be magenta/static
instead of showing actual link state. This is a known root cause under
investigation — see notes/2026-04-03-led-pipeline-investigation.md.

## Automated Demo (Unattended)

If you just want a quick PASS/FAIL without visual inspection:

```bash
sudo wedge100s-led-diag-bmc.py demo
```

This runs all 8 patterns (off → solid0-3 → rainbow → walk → passthrough) with
3 seconds between each. Each step writes the intended value and reads back the
actual value from the CPLD. Output looks like:

```
--- off ---
  off: intended=0x00 actual=0x00  [PASS]

--- solid steam=0 ---
  solid steam=0: intended=0x80 actual=0x80  [PASS]

...

=== Summary ===
Total: 8 steps, 8 passed, 0 failed
Results saved to /run/wedge100s/led_diag_results.json
```

**This only verifies the SONiC → BMC → CPLD register write path.** It does NOT
confirm the LEDs are visually correct — that requires a human at the front panel.

Inspect results afterward:
```bash
cat /run/wedge100s/led_diag_results.json | python3 -m json.tool
```

Key fields: `"all_pass": true` and each step should show `"match": true`.

## Troubleshooting

### "ERROR: could not read CPLD 0x3c via daemon (timeout)"

The bmc-daemon isn't responding. Check:

```bash
sudo systemctl status wedge100s-bmc-daemon
sudo journalctl -u wedge100s-bmc-daemon --no-pager -n 30
```

**Fix:** Restart the daemon:
```bash
sudo systemctl restart wedge100s-bmc-daemon
```

If it keeps failing, the BMC SSH connection may be down. Test manually:
```bash
ssh -o ConnectTimeout=5 root@fe80::ff:fe00:1%usb0 echo ok
```

If BMC SSH is broken, see Disaster Recovery below.

### FAIL on readback (intended != actual)

The tool retries up to 3 times automatically. If it still fails:

1. **Check if the daemon is busy.** The daemon has a 10-second sensor poll cycle.
   If your write lands during a poll, the inotify event may be coalesced. Wait
   10 seconds and retry:
   ```bash
   sleep 10 && sudo wedge100s-led-diag-bmc.py set rainbow
   ```

2. **Check BMC reachability:**
   ```bash
   sudo journalctl -u wedge100s-bmc-daemon --no-pager -n 10
   ```
   Look for "bmc_ensure_connected" or SSH errors.

3. **Restart the daemon** and retry:
   ```bash
   sudo systemctl restart wedge100s-bmc-daemon
   sleep 5
   sudo wedge100s-led-diag-bmc.py status
   ```

### LEDs don't change visually (but sysfs shows correct values)

The CPLD register write succeeded but the LEDs aren't responding. Possible causes:

- **Scan chain issue:** The CPLD-to-LED shift register chain may have a hardware
  fault. Try the walk pattern (`set walk`) — if individual ports don't light
  sequentially, the scan chain has a problem.

- **Power to LED drivers:** Some LED failures are power-related. Check PSU status
  and board voltage rails if no LEDs light at all.

### LEDs stuck on magenta/rainbow after setting passthrough

This is the known LEDUP_EN issue. The Tomahawk's LEDUP processors are disabled
(EN bit cleared after syncd init), so in passthrough mode the CPLD receives no
scan chain data and falls back to its default pattern.

**Temporary fix** (resets on syncd restart):
```bash
sudo python3 /usr/local/bin/wedge100s-led-diag.py set passthrough
```
The direct-access tool writes LEDUP_EN=1 via /dev/mem. This is not persistent.

### Stale /run/wedge100s/ files

If sysfs values look wrong or stale:
```bash
ls -la /run/wedge100s/cpld_led_ctrl /run/wedge100s/cpld_led_color
```
Check the timestamps. If they haven't updated recently, the daemon may be stuck.
Restart it:
```bash
sudo systemctl restart wedge100s-bmc-daemon
```

## Disaster Recovery (Avoiding Reboots)

### Recovering wedge100s-bmc-daemon

The daemon is a simple single-process service. It's always safe to restart:
```bash
sudo systemctl restart wedge100s-bmc-daemon
```
This does not affect pmon, syncd, or any data-plane traffic. The daemon only
manages BMC communication for sensor reads and LED control.

### Recovering from bad CPLD state

If the CPLD is left in a test mode (rainbow, walk, solid) and the tool can't
set it back to passthrough, write the .set file manually:
```bash
echo "0x02" > /run/wedge100s/led_ctrl_write.set
```
Wait 5 seconds, then verify:
```bash
cat /run/wedge100s/cpld_led_ctrl
```
Should show `2`. If the daemon is completely dead, you can also reset via
direct BMC SSH (last resort):
```bash
ssh root@fe80::ff:fe00:1%usb0 'i2cset -f -y 12 0x31 0x3c 0x02'
```

### Recovering syncd (if it crashed during LED investigation)

If syncd crashed (e.g. from bcmcmd socket interactions during advanced investigation):
```bash
sudo systemctl restart syncd
```
Wait 3 minutes for ASIC re-init. Then restart pmon:
```bash
sudo systemctl start pmon
```
Data-plane forwarding resumes after syncd finishes initializing.

**Do NOT use `docker exec syncd supervisorctl restart syncd`** — this creates
orphan syncd processes that fight over the ASIC and the dsserve socket. Always
use `systemctl restart syncd` to restart the entire container.

### Recovering BMC SSH (if connection is lost)

If the BMC is unreachable via `fe80::ff:fe00:1%usb0`:
```bash
# Check if usb0 interface is up
ip link show usb0
# If down:
sudo ip link set usb0 up
# Retry:
ssh -o ConnectTimeout=5 root@fe80::ff:fe00:1%usb0 echo ok
```

If BMC is still unreachable, try the serial console fallback:
```bash
# 57600 baud, blocking — press Enter to get login prompt
sudo picocom -b 57600 /dev/ttyACM0
# Login: root / 0penBmc
```

### When you MUST reboot

You should almost never need to reboot. The only scenarios that require it:

- **Kernel panic** (check `dmesg` for oops/panic)
- **PCI bus error** preventing ASIC access (extremely rare)
- **BMC completely unresponsive** on both SSH and serial (hardware fault)

For everything else, restarting individual services is sufficient and preserves
uptime and forwarding state.

## Quick Reference Card

```
# Health check
sudo wedge100s-led-diag-bmc.py status

# Set specific pattern (for visual verification)
sudo wedge100s-led-diag-bmc.py set off           # all dark
sudo wedge100s-led-diag-bmc.py set solid 0       # solid color 0
sudo wedge100s-led-diag-bmc.py set solid 1       # solid color 1
sudo wedge100s-led-diag-bmc.py set solid 2       # solid color 2
sudo wedge100s-led-diag-bmc.py set solid 3       # solid color 3
sudo wedge100s-led-diag-bmc.py set rainbow       # cycling rainbow
sudo wedge100s-led-diag-bmc.py set walk          # walking single LED
sudo wedge100s-led-diag-bmc.py set passthrough   # normal operation

# Automated test (3s per pattern, no user interaction)
sudo wedge100s-led-diag-bmc.py demo

# Read sysfs
cat /run/wedge100s/cpld_led_ctrl
cat /run/wedge100s/cpld_led_color
cat /run/wedge100s/led_diag_results.json | python3 -m json.tool

# Restart daemon if stuck
sudo systemctl restart wedge100s-bmc-daemon

# Emergency passthrough restore
echo "0x02" > /run/wedge100s/led_ctrl_write.set
```
