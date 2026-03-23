# PCA9535 INT_L GPIO Investigation

**Date:** 2026-03-23
**Result:** INT_L is not routed to any host CPU GPIO on this hardware. Interrupt-driven
presence detection is not possible. Polling is confirmed correct for all reference
platforms. Task 7 (4a — INT_L persistent daemon) **CANCELLED**.

---

## Hardware: GPIO chips visible to host CPU

| gpiochip    | label        | ngpio | Notes                                         |
|-------------|--------------|-------|-----------------------------------------------|
| gpiochip512 | `gpio_ich`   | 76    | x86 ICH/PCH GPIO — general-purpose platform IOs |
| gpiochip588 | `cp2112_gpio` | 8    | HID USB bridge GPIO; used only for CP2112 I/O |

Neither chip has any connection to a PCA9535 INT_L output.

The PCA9535 presence chips (I2C addresses 0x22 on bus 36, 0x23 on bus 37) sit behind
a PCA9548 mux (0x74 channels 2 and 3). Their INT_L pins are not brought out to any
testpoint, header, or GPIO input on the host CPU PCB.

---

## I2C topology barrier

Even if INT_L were physically routed to a host GPIO, the Linux `pca953x` kernel driver's
built-in IRQ support requires the PCA9535 to be directly accessible on its I2C bus at
registration time. On this platform the chips are behind a PCA9548 mux that must be
selected before each transaction. The `pca953x` driver has no mechanism to select a mux
before asserting the IRQ handler, making kernel-level interrupt-driven reads architecturally
infeasible even with a routed INT_L line.

---

## Kernel interrupt routing constraint: `noapic`

The platform boots the Linux kernel with `noapic` (confirmed in the ONIE/SONiC kernel
command line). This disables the I/O APIC and forces all interrupts through legacy XT-PIC
routing on CPU0 only. Adding new GPIO interrupt sources under these constraints is
impractical and untested on this platform.

---

## Reference platform survey

All three reference platforms were checked for any interrupt-driven presence implementation.
None found.

### ONL Wedge 100S-32X

Source: `/export/sonic/OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/`

- `onlp/builds/.../sfpi.c` — `onlp_sfpi_is_present()` calls `onlp_i2c_readb()` of the
  PCA9535 INPUT registers at 0x22/0x23 on buses 36/37. Pure polling.
- `onlp/builds/.../sfpi.c` — `onlp_sfpi_presence_bitmap_get()` reads all four INPUT
  registers (two chips × two registers) via direct I2C. Pure polling.
- Platform init (`__init__.py`) registers five PCA9548 muxes and one 24c64 EEPROM only.
  No PCA9535 kernel driver registration, no IRQ base assignment.
- **Zero references** to `gpio_request`, `request_irq`, `INT_L`, `INTL`, or `interrupt`
  anywhere in the wedge100s-32x ONL tree.

### ONL Facebook Wedge 100-32X (non-S)

Source: `/export/sonic/OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100-32x/`

Identical design to the Wedge 100S-32X for presence detection:

- `sfpi.c` polls PCA9535 at 0x22/0x23 on buses 35/36 via `onlp_i2c_readb()`.
- Platform init registers PCA9548 muxes and 24c64 EEPROM only.
- **Zero references** to GPIO interrupts anywhere in the wedge100-32x ONL tree.

The Facebook Wedge 100 and Wedge 100S-32X share the same interrupt-absent hardware
design for QSFP presence.

### SONiC Accton AS7712-32X

Source: `/export/sonic/sonic-buildimage.claude/platform/broadcom/sonic-platform-modules-accton/as7712-32x/`

The AS7712 uses a CPLD (I2C address 0x60, bus 4) with sysfs files `module_present_N`
for presence. The implementation is also polling:

- `accton_as7712_32x_sfp.c` — **zero references** to `irq`, `interrupt`, `gpio`, or
  `INT_L` in the kernel SFP module.
- `event.py` — explicitly implements `POLL_INTERVAL_IN_SEC = 1` with a `time.sleep()`
  loop; presence change detection by diffing successive reads.
- `sfputil.py`, `chassis.py` — read CPLD sysfs files; no interrupt path.
- All CPLD kernel modules (`accton_i2c_cpld.c`, etc.) — no IRQ registration.

---

## Conclusion

The Wedge 100S-32X hardware **does not route PCA9535 INT_L to any host CPU GPIO**.
The absence of a routed INT_L is consistent across:

1. Live hardware GPIO enumeration (only `gpio_ich` and `cp2112_gpio` visible)
2. ONL Wedge 100S-32X reference — polling only
3. ONL Facebook Wedge 100 reference — polling only (same design lineage)
4. SONiC AS7712-32X reference — polling only
5. I2C mux topology makes kernel pca953x IRQ support architecturally infeasible
6. `noapic` kernel boot further constrains interrupt routing

**Decision:** One-shot daemon architecture preserved; QSFP presence polling via
`wedge100s-i2c-daemon` on a 3-second systemd timer. Task 7 (4a — INT_L persistent
daemon) is **CANCELLED**.
