# OpenBMC Known Boot Warnings — Wedge 100S-32X

These messages appear on every BMC boot and are pre-existing issues in the
Facebook OpenBMC firmware. They do not affect switch operation.

## i2c-utils.sh: i2c-14 / i2c-15 not found

```
/usr/local/bin/i2c-utils.sh: line 34: /sys/class/i2c-dev/i2c-14/device/new_device: No such file or directory
/usr/local/bin/i2c-utils.sh: line 34: /sys/class/i2c-dev/i2c-15/device/new_device: No such file or directory
```

**Cause:** `i2c-utils.sh` hard-codes bus numbers for a chassis variant with additional mux-expanded
buses (i2c-14, i2c-15). The Wedge 100S-32X BMC only has buses i2c-0 through i2c-13
(14 buses total from the AST2400 on-chip I2C controllers). These lines silently fail.

**Impact:** None. The devices that would be instantiated on i2c-14/15 are absent or
already handled via another bus.

## S90sensor-setup.sh: adc5_en / adc6_en Permission denied

```
/etc/rcS.d/S90sensor-setup.sh: line 48: /sys/devices/platform/ast_adc.0/adc5_en: Permission denied
/etc/rcS.d/S90sensor-setup.sh: line 49: /sys/devices/platform/ast_adc.0/adc6_en: Permission denied
```

**Cause:** AST ADC channels 5 and 6 sysfs nodes are read-only in this BMC kernel build.
The `sensor-setup.sh` script tries to enable them by writing, which fails.

**Impact:** ADC channels 5/6 monitoring is disabled. The remaining channels (0-4) are
sufficient for the fan/PSU temperature monitoring that `wedge100s-bmc-daemon` relies on.

## Normal boot indicators to look for

These confirm a healthy BMC boot:
- `i2c i2c-12: new_device: Instantiated device syscpld at 0x31` — BMC CPLD driver loaded
- `powering on microserver ... done` — switch power-on sequenced
- `usb0: HOST MAC 02:00:00:00:00:02` / `self ethernet address: 02:00:00:00:00:01` — USB CDC-Ethernet up with correct MACs for IPv6 link-local access
