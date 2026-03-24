# BMC GPIO Architecture — Accton Wedge 100S-32X
**Date:** 2026-03-23
**Source:** BMC boot log (BMC-gpio.txt) + live hardware inspection

---

## Named GPIOs Relevant to Platform Port/LED/I2C Subsystems

| Shadow name | GPIO | Chip pin | Dir | Value | Role |
|---|---|---|---|---|---|
| `BMC_CPLD_QSFP_INT` | gpio31 | GPIOD7 | in | 0 (asserted) | QSFP presence interrupt from syscpld → BMC |
| `QSFP_LED_POSITION` | gpio59 | GPIOH3 | in | 1 | Board strap: LED chain scan direction |
| `LED_PWR_BLUE` | gpio40 | GPIOE5 | out | — | Front-panel power indicator (blue) — separate from port LEDs |
| `PANTHER_I2C_ALERT_N` | gpio8 | GPIOB0 | in | — | BCM56960 I2C alert to BMC |
| `BMC_CPLD_POWER_INT` | gpio97 | GPIOQ4 | in | — | Power-related interrupt from syscpld |
| `TH_POWERUP` | gpio51 | GPIOM3 | out | — | Tomahawk power-up control |
| `SMB_ALERT` | gpio49 | GPIOM1 | in | — | SMBus alert |

---

## BMC_CPLD_QSFP_INT — QSFP Presence Interrupt

**Path:** `/sys/class/gpio/gpio31/` (shadow: `/tmp/gpionames/BMC_CPLD_QSFP_INT`)
**Direction:** input
**Polarity:** active-low (0 = interrupt asserted)
**Current state:** 0 (interrupt pending — QSFPs are present and the interrupt has not been cleared by a BMC-side read)
**Edge config:** `none` (no kernel interrupt handler registered)

### What it means

The syscpld aggregates the PCA9535 presence INT# signals and drives this GPIO. When any QSFP module is inserted or removed, the PCA9535 INT# asserts → syscpld logic propagates to GPIOD7 → value drops to 0.

The interrupt self-clears when the PCA9535 INPUT register is read. On the host side, `wedge100s-i2c-daemon` reads the PCA9535 every 3 seconds via hidraw (which clears the interrupt from the PCA9535's perspective). However, the GPIO only reflects state from the syscpld's view of the INT# line, so it may remain 0 while any module is seated.

### Architecture opportunity

The bmc-daemon can poll `gpio31/value` via SSH and write it to `/run/wedge100s/qsfp_int`. If the i2c-daemon sees this file transition to 0, it can do an immediate presence scan rather than waiting up to 3 seconds. This reduces insertion detection latency.

Read via SSH: `cat /sys/class/gpio/gpio31/value`
Or named: `. /usr/local/bin/gpio-utils.sh && gpio_get_value BMC_CPLD_QSFP_INT`

---

## QSFP_LED_POSITION — LED Chain Orientation Strap

**Path:** `/sys/class/gpio/gpio59/` (shadow: `/tmp/gpionames/QSFP_LED_POSITION`)
**Direction:** input (board strap, tied at PCB level)
**Current value:** 1

This GPIO is a hardware strap that indicates the physical orientation of the QSFP LED scan chains relative to the front panel port numbering. Value=1 on this board.

**Not used by any BMC software** — no references in `/usr/local/bin/` or `/usr/local/lib/`.
**Relevant to the current LED color/mapping investigation**: the BCM LEDUP0/1 chain scan direction may depend on this strap. The LED program's port-order remap table in `led_proc_init.soc` should match the physical LED wiring implied by this strap.

The bmc-daemon should expose this via `/run/wedge100s/qsfp_led_position` so the host can read the board's intended LED orientation without needing to know the GPIO number.

---

## syscpld Attributes for CP2112 and Mux Control

These attributes on the BMC syscpld (`/sys/bus/i2c/devices/12-0031/`) give the BMC control over host-side I2C infrastructure:

| Attribute | Function | Script |
|---|---|---|
| `i2c_flush_en` | Flush CP2112 I2C state (pulse 1→0) | `cp2112_i2c_flush.sh` |
| `usb2cp2112_rst_n` | Hard-reset CP2112 USB device (write 0) | — |
| `i2c_mux0_rst_n` – `i2c_mux3_rst_n` | Reset QSFP PCA9548 muxes | `reset_qsfp_mux.sh` |

These are recovery mechanisms. `cp2112_i2c_flush.sh` and `reset_qsfp_mux.sh` are already present on the BMC. They should be invokable via SSH from the SONiC host when the i2c-daemon detects a bus hang (e.g., via `ast-i2c` recovery messages in dmesg).

---

## SSH Path to BMC

**Address:** `root@192.168.88.13`
**Auth:** key-based (authorized_keys populated)
**Key note:** authorized_keys is cleared on BMC reboot — requires `ssh-copy-id` after each BMC reset.

SSH enables non-blocking, structured command execution to the BMC without the TTY prompt-matching overhead. Replaces the fragile ttyACM0 session in `wedge100s-bmc-daemon`.

---

## Architecture: bmc-daemon Refactor with SSH

### Current (TTY-based)
```
wedge100s-bmc-daemon → /dev/ttyACM0 → BMC shell → i2cget/i2cset/cat sysfs
```
Problems: blocking I/O, prompt-matching heuristics, login sequence, serial console occupied.

### Proposed (SSH-based)
```
wedge100s-bmc-daemon → ssh root@192.168.88.13 'command' → BMC shell → sysfs
```
Benefits: non-blocking, structured output, no login sequence, parallel commands possible.

### New /run/wedge100s/ files via SSH

| File | Source command | Update rate | Consumer |
|---|---|---|---|
| `syscpld_led_ctrl` | `i2cget -f -y 12 0x31 0x3c` | every 10 s | inspection, LED init logic |
| `qsfp_int` | `cat /sys/class/gpio/gpio31/value` | every 10 s | i2c-daemon (fast-scan trigger) |
| `qsfp_led_position` | `cat /sys/class/gpio/gpio59/value` | once at boot | LED mapping validation |

### Write-request pattern for syscpld_led_ctrl

- Platform code (Python) writes desired value to `/run/wedge100s/syscpld_led_ctrl.set`
- bmc-daemon reads `.set` file on next invocation → executes `ssh root@bmc 'i2cset -f -y 12 0x31 0x3c <value>'` → removes `.set` file
- Keeps all I2C writes in bmc-daemon (centralized service), platform code only writes files

### LED init at boot (runtime)

`accton_wedge100s_util.py install` writes `/run/wedge100s/syscpld_led_ctrl.set` = `0x02`
bmc-daemon picks it up at t=15s and clears the rainbow.

### LED init at install (ONIE installer hook)

`installer/platforms/x86_64-accton_wedge100s_32x-r0` adds `if [ "$install_env" = "onie" ]` block:
- Direct SSH to BMC (if available) or TTY fallback
- Sends `i2cset -f -y 12 0x31 0x3c 0x02` immediately at install time

---

## Other BMC Devices Identified

| Device | Driver | Role |
|---|---|---|
| `4-0033` | COME sensor IC | COM Express module voltages/temps/identity (unused by SONiC) |
| `6-002f` | `pcf8574` | 8-bit I2C GPIO expander — purpose TBD |
| `6-0051` | `24c64` | BMC-accessible EEPROM |
| `2-003a` | `pwr1014a` | Power IC with `mod_hard_powercycle` capability |
| `7-0050/51/52` | — | PSU EEPROMs or PMBus (i2c-7 = PSU mux bus) |
| `7-006f` | — | TBD |
| `8-0048/49` | TMP75? | Temperature sensors on i2c-8 |
