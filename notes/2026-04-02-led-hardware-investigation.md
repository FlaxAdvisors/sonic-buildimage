# LED Hardware Investigation — 2026-04-02

## Problem
Front panel QSFP port LEDs show solid magenta. System LEDs (SYS1/SYS2) are correctly green.

## Hardware Layers

### Layer 1: BMC SYSCPLD (i2c-12, addr 0x31)
- **model_id**: 0x0 = "wedge100" (shared CPLD between wedge100 and wedge100s)
- **board_rev**: 0x2
- **cpld_rev**: 6, cpld_sub_rev: 0x65

**Register 0x3c** — LED control (power-on default: 0xe0 = rainbow test mode)

| Bit | Sysfs attr | Default | Current | Function |
|-----|-----------|---------|---------|----------|
| 7 | led_test_mode_en | 1 | 0 | CPLD LED test pattern |
| 6 | led_test_blink_en | 1 | 0 | Blink during test |
| 5:4 | th_led_steam | 2 | 0 | Test stream select |
| 3 | walk_test_en | 0 | 0 | Walking LED test |
| 1 | th_led_en | 0 | **1** | BCM LEDUP passthrough enable |
| 0 | th_led_clr | 0 | 0 | Clear LEDUP scan chain |

**Register 0x3d** = 0x40 — LED test color/number (only active when th_led_steam != 0)
**Register 0x3e** = 0x02 — SYS1 LED (green) — also visible via host CPLD 1-0032
**Register 0x3f** = 0x02 — SYS2 LED (green) — also visible via host CPLD 1-0032

### Layer 2: Host CPLD (i2c-1, addr 0x32)
- Driver: `wedge100s_cpld` (kernel module)
- Exposes only: `led_sys1`, `led_sys2`, `cpld_version` (2.6), `psu1_present`, `psu2_present`, `psu1_pgood`, `psu2_pgood`
- LED encoding: 0x00=off, 0x01=red, 0x02=green, 0x04=blue, +0x08=blinking
- No per-port LED control from host CPLD

### Layer 3: BCM56960 Tomahawk LEDUP

**Available LEDUP registers** (BCM shell `listreg CMIC_LEDUP`):
- `CMIC_LEDUPx_CTRL` — processor control (enable, scan timing)
- `CMIC_LEDUPx_DATA_RAM[]` — per-port status data + scan chain output
- `CMIC_LEDUPx_PROGRAM_RAM[]` — LED bytecode
- `CMIC_LEDUPx_PORT_ORDER_REMAP_*` — physical LED position mapping
- `CMIC_LEDUPx_CLK_DIV` — scan chain clock divider
- `CMIC_LEDUPx_CLK_PARAMS` — refresh cycle period
- `CMIC_LEDUPx_STATUS` — program counter, running state
- `CMIC_LEDUPx_TM_CONTROL` — test mode
- `CMIC_LEDUPx_SCANOUT_COUNT_UPPER` — scan chain bit count
- `CMIC_LEDUPx_SCANCHAIN_ASSEMBLY_ST_ADDR` — scan output start address in DATA_RAM
- Three LEDUP processors: LEDUP0, LEDUP1, LEDUP2

**LEDUP0 state:**
- CTRL: LEDUP_EN=1, SCAN_START_DELAY=0x2a, INTRA_PORT_DELAY=4
- STATUS: RUNNING=0 (idle between refresh), PC=0x42
- TM_CONTROL: TM=0 (no BCM test mode)
- CLK_DIV: LEDCLK_HALF_PERIOD=0x64
- CLK_PARAMS: REFRESH_CYCLE_PERIOD=0x5b8d80
- SCANCHAIN_ASSEMBLY_ST_ADDR: 0x80

**LEDUP1 state:**
- CTRL: LEDUP_EN=1, SCAN_START_DELAY=0x1e, INTRA_PORT_DELAY=4

## ROOT CAUSE: LEDUP DATA_RAM

**LEDUP0 DATA_RAM[0..31]** — per-port status bytes:
```
Port type   Entries            Value   Binary
ce (100G)   0,8,16,24         0xb8    10111000  — Link=1, FD=1, Speed=11
xe (even)   2,4,6,10,12,...   0xf8    11111000  — Link=1, FC=1, FD=1, Speed=11
xe (odd)    1,3,5,7,9,...     0x80    10000000  — Link=1 only
```

**ALL 32 entries have bit 7 (Link Status) = 1.**

- These values are populated by the BCM hardware MAC/SERDES
- Cannot be cleared via `setreg` (values snap back immediately)
- Even with `led auto off`, LED processors stopped, values persist
- Because all ports are `!ena` (admin-down), the SERDES reports default link-up

## Chain of Causation

1. `config_db.json` has no PORT table (regression from gen-l3-config.py on 2026-03-30)
2. SONiC configures zero ports → BCM has all ports `!ena`
3. BCM MAC/SERDES still populates LEDUP DATA_RAM with link=1 for all ports
4. LED bytecode sees link=1 → outputs "link up" pattern on both LEDUP0 and LEDUP1
5. Both scan chains active → both LED colors lit simultaneously → magenta

## Physical LED Hardware (unknown/unverified)

- QSFP28 cages are stacked (upper/lower), with LEDs between them
- LEDs are described as "pairs of up/down pointing triangles on left and right side"
- Left side up/down triangles may indicate link for upper/lower port
- Right side up/down triangles may indicate traffic for upper/lower port
- Physical LED colors driven by LEDUP0 vs LEDUP1 are UNKNOWN
- Active-high vs active-low wiring is UNKNOWN
- When both LEDUP0 and LEDUP1 drive their channels simultaneously: magenta

## What We Verified Works

- Walk test (walk_test_en=1 on CPLD): produces visible sweeping pattern (confirmed by on-site user)
- This proves the physical LED hardware responds to CPLD control
- System LEDs (SYS1/SYS2): correctly green, responsive to CPLD writes

## Existing LED Code

- **ONL**: Only controls SYS1/SYS2 system LEDs. No per-port LED code.
- **SONiC**: `led_proc_init.soc` loads AS7712-identical bytecode with Wedge100S-specific REMAP tables. `led auto on` enables hardware port status → LEDUP data RAM.
- **No FBOSS source** available locally.
- **No LED polarity configuration** in any BCM config file.

## Open Questions for Spec Research

1. BCM56960 LEDUP processor instruction set (opcodes, register map)
2. LEDUP DATA_RAM bit assignments (confirmed: bit 7 = link, but need full map)
3. Physical QSFP cage LED wiring (LEDUP0 = what color? LEDUP1 = what color?)
4. Active-high vs active-low (walk test behavior suggests CPLD drives differently than BCM)
5. How FBOSS controls these LEDs on the same hardware
6. Whether the LED bytecode can be modified to check admin-enable or different status bits
