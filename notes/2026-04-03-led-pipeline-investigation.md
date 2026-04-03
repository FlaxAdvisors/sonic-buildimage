# LED Pipeline Investigation — 2026-04-03

## Summary

Full hardware investigation of the Wedge 100S-32X LED pipeline via BCM56960 LEDUP
register access (bcmcmd and /dev/mem) and CPLD register reads.

## Step 1: LEDUP Processor State

### bcmcmd "led 0 status" / "led 1 status"
(verified on hardware 2026-04-03)

```
# Both LEDUP0 and LEDUP1 show port mappings with LI (link indicator) on some ports.
# Example ports with LI on LEDUP0: ce0(1), ce4(17), ce8(34), ce22(102), xe86(118), xe87(119)
# Format: port_num  port_name  LI/blank  speed  duplex  proc:slot
# e.g. "1  ce0  LI  10G HD 0:5"
```

Result: Port-to-LEDUP slot mapping is populated. Some ports show LI (link indicator).

## Step 2: LEDUP Control Registers

### bcmcmd "getreg CMIC_LEDUP0_CTRL" / "getreg CMIC_LEDUP1_CTRL"
(verified on hardware 2026-04-03)

```
CMIC_LEDUP0_CTRL.cmic0[9][0x20000]=0x2a9: <LEDUP_SCAN_START_DELAY=0x2a,
   LEDUP_SCAN_INTRA_PORT_DELAY=4,LEDUP_EN=1>

CMIC_LEDUP1_CTRL.cmic0[9][0x21000]=0x1e9: <LEDUP_SCAN_START_DELAY=0x1e,
   LEDUP_SCAN_INTRA_PORT_DELAY=4,LEDUP_EN=1>
```

Note: bcmcmd showed EN=1 because the `led start` command was run during the SDK init
window. After syncd initialization completes, direct /dev/mem reads show EN=0 (see
Step 2b).

### /dev/mem direct read (post-init, before manual fix)
(verified on hardware 2026-04-03)

```
CMIC_LEDUP0_CTRL (0x20000): 0x000002a8  EN=0
CMIC_LEDUP1_CTRL (0x21000): 0x000001e8  EN=0
```

**KEY FINDING: LEDUP_EN bit (bit 0) clears after syncd startup completes.**
The `led 0 start` / `led 1 start` commands in the SOC file run during SDK init via
the diag shell, but the LEDUP_EN bit gets cleared sometime after initialization.

## Step 2b: DATA_RAM Check
(verified on hardware 2026-04-03)

```
CMIC_LEDUP0_DATA_RAM(0)=0xb8 → 0xf8 (post-restart)
```

DATA_RAM bytes for all 128 entries (0-127):
- Even entries: 0xf8 = LINK + FD + 10G+
- Odd entries:  0x80 = LINK only

This is **static initialization data**, not real link status. All 128 entries show
LINK=1 even though `show interfaces status` shows all ports Oper=down.

## Step 3: Bytecode Verification

### bcmcmd "led 0 dump" / "led 1 dump"
(verified on hardware 2026-04-03)

Both LEDUP0 and LEDUP1 have identical bytecode loaded:
```
02 fd 42 80 02 ff 42 00 02 fe 42 00 02 fa 42 e0
02 fb 42 40 06 f9 d2 00 74 1e 02 f9 42 03 67 ac
67 c3 67 52 86 fe 67 c3 67 52 86 fe 67 c3 67 52
86 fe 67 c3 67 52 86 fe 06 fb d6 fe 74 1e 86 fc
...
```
This matches the AS7712-32X bytecode exactly (expected — same ASIC).
232/256 non-zero program bytes.

### BAR2 Register Map Discovery
(verified on hardware 2026-04-03)

The `wedge100s_ledup.py` library had **incorrect register offsets**:
```
WRONG (iProc space):          CORRECT (CMIC space):
LEDUP0_CTRL    = 0x34000      LEDUP0_CTRL    = 0x20000
LEDUP0_STATUS  = 0x34004      LEDUP0_STATUS  = 0x20004
LEDUP0_PROG    = 0x34100      LEDUP0_PROG    = 0x20800
LEDUP0_DATA    = 0x34800      LEDUP0_DATA    = 0x20400
LEDUP1_CTRL    = 0x34400      LEDUP1_CTRL    = 0x21000
LEDUP1_STATUS  = 0x34404      LEDUP1_STATUS  = 0x21004
LEDUP1_PROG    = 0x34500      LEDUP1_PROG    = 0x21800
LEDUP1_DATA    = 0x34C00      LEDUP1_DATA    = 0x21400
```

The correct addresses were discovered by scanning BAR2 for the known bytecode
signature `02 FD 42 80` (found at offset 0x20800).

## Step 4: LED Auto Mode

### bcmcmd "led auto on"
(verified on hardware 2026-04-03)

The `led auto on` command was accepted during the SDK init window.
However, `led auto` is a **software-level SDK feature** — it runs a periodic timer
in the BCM SDK to copy port link status into DATA_RAM. It is NOT a hardware register.
After the diag shell becomes inactive, the auto update stops.

## Step 5: Stop/Start Cycle

### bcmcmd "led 0 stop" / "led 0 start" / "led 1 stop" / "led 1 start"
(verified on hardware 2026-04-03)

Commands accepted. The start command sets LEDUP_EN=1. However, this state does not
persist — see Root Cause.

## Step 6: SOC File and LED Files

### led_proc_init.soc
(verified on hardware 2026-04-03)

```
$ ls -la /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc
-rw-r--r-- 1 root root 7366 Feb 20  2017

$ sudo docker exec syncd find /usr/share/sonic/platform/ -name "*led*"
/usr/share/sonic/platform/led_proc_init.soc
/usr/share/sonic/platform/plugins/led_control.py
```

SOC file exists and contains:
1. PORT_ORDER_REMAP registers for positions 0-63 (both LEDUP0 and LEDUP1)
2. `led 0 prog <bytecode>`  
3. `led 0 start`
4. `led 1 prog <bytecode>`
5. `led 1 start`
6. `led auto on`

### start_led.sh
(verified on hardware 2026-04-03)

```bash
# If this platform has an initialization file, load it
if [[ -r "$LED_PROC_INIT_SOC" && ! -f /var/warmboot/warm-starting ]]; then
    wait_syncd
    /usr/bin/bcmcmd -t 60 "rcload $LED_PROC_INIT_SOC"
fi
```

The ledinit supervisor process runs `start_led.sh` which calls `bcmcmd -t 60 "rcload ..."`.

## Step 7: Additional Diagnostics

### CPLD LED Control Register
(verified on hardware 2026-04-03)

```
/run/wedge100s/cpld_led_ctrl: 2   (0x02 = PASSTHROUGH mode)
/run/wedge100s/cpld_led_color: 64  (0x40)
```

Via BMC direct read:
```
CPLD 0x3c = 0x02
  test_mode_en:   False
  th_led_en:      True   (passthrough enabled)
```

CPLD is correctly configured for Tomahawk passthrough.

### bcmcmd Diag Shell Lifetime
(verified on hardware 2026-04-03)

**CRITICAL FINDING:** The BCM SDK diag shell (dsserve socket) becomes unresponsive
after syncd initialization completes (~60s after boot). After that:
- `bcmcmd` returns exit code 62 ("polling socket timeout")
- Socket connects but only echoes input; no "drivshell>" prompt
- `led auto on` (software feature) stops functioning
- `led start` (LEDUP_EN=1) gets cleared

The `ledinit` process frequently fails with exit code 62 because it runs too late
(after the diag shell window closes).

### LEDUP Bytecode Execution (post-manual-enable)
(verified on hardware 2026-04-03)

After manually setting LEDUP_EN=1 via /dev/mem write to 0x20000:
```
LEDUP0_CTRL: 0x000002a9 (EN=1)
LEDUP1_CTRL: 0x000001e9 (EN=1)
LEDUP0_STATUS: 0x00000042 (RUNNING)
LEDUP1_STATUS: 0x00000042 (RUNNING)
```

Scratch register monitoring shows bytecode IS executing:
```
[0] F9=0x03 FC=0xfa FD=0x9c FE=0x40
[1] F9=0x03 FC=0x16 FD=0x9c FE=0x40
[2] F9=0x03 FC=0x32 FD=0x9c FE=0x40
[3] F9=0x03 FC=0x4f FD=0x9c FE=0x40
[4] F9=0x03 FC=0x6b FD=0x9c FE=0x40
```
FC (port counter) is changing rapidly — bytecode is running and iterating ports.

---

## Root Cause

**Three independent issues in the LED pipeline:**

### Issue 1: LEDUP_EN clears after SDK init (PRIMARY)
The `led 0 start` / `led 1 start` commands in the SOC file successfully set
LEDUP_EN=1 during the SDK initialization window. However, the LEDUP_EN bit
(bit 0 of CMIC_LEDUP0_CTRL / CMIC_LEDUP1_CTRL) is cleared to 0 after syncd
finishes initializing. This means the LEDUP processors stop running and produce
no scan chain output. With CPLD in passthrough mode and no scan chain data from
the Tomahawk, the CPLD either shows all-off or falls through to the test pattern.

### Issue 2: led auto stops after diag shell closes
The `led auto on` command (last line of SOC file) enables a software timer in the
BCM SDK to periodically copy port link status into DATA_RAM. After the diag shell
closes (~60s), this timer stops. Even if LEDUP_EN were maintained, DATA_RAM would
contain stale initialization data (all ports showing LINK=1) rather than real link
status.

### Issue 3: ledinit frequently fails (exit code 62)
The `start_led.sh` script uses `bcmcmd` to load the SOC file. If syncd completes
initialization before bcmcmd connects to the diag shell socket, bcmcmd times out
(exit 62) and the SOC file is never loaded. This is a race condition — sometimes
the ledinit window is missed entirely.

### Issue 4: wedge100s_ledup.py has wrong register offsets
The `/dev/mem` access library uses iProc offsets (0x34000+) instead of CMIC offsets
(0x20000+). All reads/writes through this library access the wrong memory addresses.

---

## Recommended Fix

### Fix 1: Write LEDUP_EN via /dev/mem in a persistent service
Create a lightweight daemon or systemd oneshot that:
1. Writes LEDUP_EN=1 to CMIC_LEDUP0_CTRL and CMIC_LEDUP1_CTRL via /dev/mem
2. Runs after syncd has fully initialized (not during init window)
3. Periodically verifies LEDUP_EN stays set

### Fix 2: Update DATA_RAM with real link status via /dev/mem
Since `led auto on` only works during the diag shell window, implement a Python
daemon that:
1. Reads port link status from SONiC (COUNTERS_DB or `show interfaces status`)
2. Writes correct link status to DATA_RAM entries via /dev/mem
3. Runs periodically (every 1-2 seconds)

### Fix 3: Fix wedge100s_ledup.py register offsets
Update the register base addresses from iProc space to CMIC space:
```python
# CORRECT offsets for Tomahawk via BAR2
LEDUP0_CTRL = 0x20000
LEDUP0_STATUS = 0x20004
LEDUP0_PROGRAM_RAM_BASE = 0x20800
LEDUP0_DATA_RAM_BASE = 0x20400
LEDUP1_CTRL = 0x21000
LEDUP1_STATUS = 0x21004
LEDUP1_PROGRAM_RAM_BASE = 0x21800
LEDUP1_DATA_RAM_BASE = 0x21400
```

### Fix 4: Remove dependency on bcmcmd for LED init
Since the diag shell is unreliable, load bytecode and set LEDUP registers
entirely via /dev/mem. Parse the SOC file for bytecode and remap tables, then
write directly to BAR2 registers. This eliminates the race condition.

### Priority Order
1. Fix 3 (register offsets) — prerequisite for everything else
2. Fix 4 (remove bcmcmd dependency) — eliminates race condition
3. Fix 1 (persistent LEDUP_EN) — immediate fix for LED output
4. Fix 2 (DATA_RAM updates) — correct link-responsive LED behavior
