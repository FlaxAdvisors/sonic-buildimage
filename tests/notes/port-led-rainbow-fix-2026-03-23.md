# Port LED Rainbow Fix — Accton Wedge 100S-32X

**Investigation date:** 2026-03-23
**Status:** Root cause identified; one-time fix applied to hardware; persistence not yet implemented

---

## Root Cause

The port LED rainbow animation is driven by the **BMC syscpld** at `i2c-12 / 0x31` on the BMC bus.
ONIE sets this CPLD to "LED test mode" with the rainbow stream enabled. SONiC (and any other NOS)
must explicitly disable test mode and enable Tomahawk LEDUP passthrough at boot.

### Hardware path

```
BCM LEDUP0/1 (host side)
    ↓
syscpld register 0x3c, bit[1] = th_led_en  ← must be 1 for BCM data to reach physical LEDs
    ↓
Physical QSFP port LEDs (front panel)
```

When `led_test_mode_en` (bit7) is set by ONIE, the syscpld injects its own RGB cycle
(red → blue → green → …) into the LED drive path, overriding BCM LEDUP output entirely.

---

## BMC syscpld register 0x3c — LED control

| Bit | Attribute | ONIE value | SONiC target |
|-----|-----------|-----------|--------------|
| 7 | `led_test_mode_en` | 1 | 0 |
| 6 | `led_test_blink_en` | 1 | 0 |
| [5:4] | `th_led_steam` | 2 (all-LED stream) | 0 |
| 3 | `walk_test_en` | 0 | 0 |
| 1 | `th_led_en` | 0 | **1** |
| 0 | `th_led_clr` | 0 | 0 |

- ONIE state: `0xe0` — test mode + blinking + all-LED stream cycling, BCM LEDUP disabled
- SONiC target: `0x02` — test modes off, BCM LEDUP enabled

---

## Devices identified (BMC I2C bus)

| Device | Identity | Role |
|--------|----------|------|
| `12-0031` | `syscpld` | BMC system CPLD — port LED mode control, board resets, PSU presence |
| `8-0033` | fan controller | Fan tray presence, fan RPM (already used by bmc-daemon) |
| `4-0033` | COME sensor IC | COM Express module voltages/temps/identity (unused by SONiC) |
| `9-0020` | `tpm_i2c_infineon` | TPM chip (unused) |

**The `4-0033` is NOT a port LED controller** — it is the COM Express module system info chip
(exposes `in0`–`in4` voltages, `temp1`/`temp2`, `product_name`, `serial_number`, `mac`, `version`).

---

## Fix Applied (2026-03-23, verified on hardware)

```bash
# From BMC (root@192.168.88.13):
echo 0 > /sys/bus/i2c/devices/12-0031/led_test_mode_en
echo 0 > /sys/bus/i2c/devices/12-0031/led_test_blink_en
echo 0 > /sys/bus/i2c/devices/12-0031/th_led_steam
echo 1 > /sys/bus/i2c/devices/12-0031/th_led_en
# Result: register 0x3c = 0x02; rainbow stopped; BCM LEDUP active
```

Equivalent single-command form (for C daemon use):
```bash
i2cset -f -y 12 0x31 0x3c 0x02
```

---

## Persistence Requirement

The syscpld register reverts to `0xe0` after:
- BMC power cycle / reboot
- ONIE re-run

The fix must be applied at every SONiC boot via the BMC TTY path
(`/dev/ttyACM0`, already used by `wedge100s-bmc-daemon`).

### Implementation options

**Option A — Add to `wedge100s-bmc-daemon` as first-run init command**
The C daemon already opens a TTY session to the BMC. Add `i2cset -f -y 12 0x31 0x3c 0x02`
as an initialization step executed before the telemetry collection loop.

**Option B — New `wedge100s-bmc-init.service` one-shot**
A dedicated oneshot systemd service (Before=pmon.service) that sends the LED init
command over TTY. Cleanest separation but requires BMC TTY login.

**Option C — Piggyback on bmc-daemon every 10s**
Idempotent; ensures recovery if BMC reboots during runtime. Adds ~0ms overhead
(single i2cset to an already-open TTY session). Recommended for simplicity.

---

## BCM LED Processor Confirmation

The BCM LED program is correctly loaded:
- `led 0 dump` confirms our bytecode is present in LEDUP0
- `LEDUP_EN=1` confirms both processors active
- LEDUP0 (chain A) = green; LEDUP1 (chain B) = amber
- `led_proc_init.soc` file at `device/accton/x86_64-accton_wedge100s_32x-r0/` is correct

The BCM can display: off, green (link up), amber (link up different speed), or green+amber.
No blue or red from BCM LEDUP — that's hardware limitation of the two single-color chains.

---

## Notes on SUBSYSTEMS_LED.md

The existing SUBSYSTEMS_LED.md documents only SYS1/SYS2 (host CPLD at 0x32).
It should be updated to document:
- The per-port LED path via `syscpld` register 0x3c
- The BCM LEDUP chain capabilities and limitations
- The boot initialization requirement
