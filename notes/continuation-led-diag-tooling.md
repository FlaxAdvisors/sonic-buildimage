# Continuation Prompt: LED Diagnostic Tooling & Daemon Fix

**Date:** 2026-04-02
**Branch:** wedge100s
**Last commit:** 9952e32b8 — `docs: add LED diagnostic tooling design spec and LEDUP utilities`

---

## Where We Left Off

The brainstorming/design phase for LED diagnostic tooling is **complete**. The approved design spec is at:

```
docs/superpowers/specs/2026-04-02-led-diag-tooling-design.md
```

**Next step:** User needs to review the spec, then invoke the `writing-plans` skill to create the implementation plan. The brainstorming skill checklist is at step 8 (user reviews written spec). Once approved, step 9 is to invoke `writing-plans`.

The file `docs/Opus-LEDs-Notes.md` was fully incorporated into the spec and deleted from disk (was never git-tracked).

---

## What To Build (summary of spec)

**`utils/wedge100s-led-diag.py`** — single Python 3 script, no external deps, runs as root on SONiC target.

Commands:
- `status` — dump CPLD 0x3c + LEDUP CTRL + DATA_RAM
- `set rainbow` — CPLD test mode (0xe0)
- `set passthrough` — CPLD 0x02, reload LED bytecode from `led_proc_init.soc`, `led auto on`
- `set all-off` — disable LEDUP, zero DATA_RAM
- `set color <color>` — software-drive all ports to a color
- `set port <n> <color>` — single port
- `probe` — cycle color combos, discover physical color mapping

All ASIC access via PCIe BAR2 `/dev/mem` mmap (no bcmcmd). CPLD access via BMC SSH (auto-detects daemon vs direct).

Shared library: `utils/wedge100s_ledup.py` — BAR2 mmap, register R/W, bytecode parser.

Also: investigate and fix dsserve/bcmcmd socket hang that prevents ledinit from loading LED bytecode.

---

## Key Technical Facts

### Hardware State (as of 2026-04-02)
- BCM56960 PCI `0000:06:00.0`, BAR2 = 0xfb000000, size 8MB
- LEDUP0/1 CTRL = 0x00 (disabled), all PROGRAM_RAM = 0, all DATA_RAM = 0
- CPLD 0x3c = 0x02 (passthrough mode, but no scan chain data to pass)
- ledinit failed silently (bcmcmd can't connect to dsserve socket)
- 12 ports link-up after PORT table restore + deploy.py breakouts
- deploy.py config saved: 3 breakouts, PortChannel1, VLANs 10/999

### BCM LEDUP Register Offsets (BAR2)
| Register | Offset |
|----------|--------|
| LEDUP0_CTRL | 0x34000 |
| LEDUP0_PROGRAM_RAM(n) | 0x34100 + 4*n |
| LEDUP0_DATA_RAM(n) | 0x34800 + 4*n |
| LEDUP1_CTRL | 0x34400 |
| LEDUP1_PROGRAM_RAM(n) | 0x34500 + 4*n |
| LEDUP1_DATA_RAM(n) | 0x34C00 + 4*n |

### dsserve Problem
- dsserve creates `/var/run/sswsyncd/sswsyncd.socket`, forks syncd, enters `do_wait`
- syncd never accepts on the socket — 43 threads, none polling diag fd
- bcmcmd connects → timeout every time
- ledinit runs `led_proc_init.soc` via bcmcmd → exits 0 after 5s (false success)
- Forwarding works fine — only diag shell is broken

### Existing Utilities (committed)
- `utils/read_ledup_mmap.py` — reads LEDUP DATA_RAM via BAR2 (proven working)
- `utils/read_ledup.sh` — reads via bcmcmd (doesn't work due to dsserve hang)

### LED Bytecode
- `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/led_proc_init.soc`
- AS7712-identical bytecode for LEDUP0 and LEDUP1
- PORT_ORDER_REMAP tables differ between processors for positions 32-63
- Ends with `led auto on`
- Bytecode is correct, just never got loaded

### BMC CPLD 0x3c Bit Layout
| Bit | Function | SONiC value |
|-----|----------|------------|
| 7 | led_test_mode_en | 0 |
| 6 | led_test_blink_en | 0 |
| 5:4 | th_led_steam | 0 |
| 3 | walk_test_en | 0 |
| 1 | th_led_en (passthrough) | 1 |
| 0 | th_led_clr | 0 |

Power-on default: 0xe0 (rainbow). SONiC target: 0x02 (passthrough).

### Port LED Color Model
- CPLD test mode: CPLD drives rainbow pattern independent of BCM
- BCM passthrough: LEDUP0 + LEDUP1 each drive 1 bit per port = 4 combinations
- LEDUP0=1, LEDUP1=1 confirmed = magenta
- Other 3 combinations unknown (probe command will discover)

---

## Files to Read First

1. `docs/superpowers/specs/2026-04-02-led-diag-tooling-design.md` — the full approved spec
2. `utils/read_ledup_mmap.py` — existing BAR2 mmap code to build on
3. `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/led_proc_init.soc` — LED bytecode
4. `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/utils/wedge100s-bmc-daemon.c` — BMC daemon with .set dispatch

---

## Instructions for Continuation

1. Ask user if they've reviewed the spec and if they approve
2. If approved, invoke the `writing-plans` skill to create the implementation plan
3. The implementation plan should cover: shared library, diagnostic tool CLI, dsserve investigation, ledinit fix/fallback
4. Do NOT start coding until the implementation plan is written and approved
