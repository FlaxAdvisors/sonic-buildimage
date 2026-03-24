# PF-05 — I2C/QSFP Daemon: Plan

## Problem Statement

32 QSFP28 cages need presence detection and EEPROM access for `xcvrd` and the
SONiC transceiver infrastructure. The physical path is:

```
Host → CP2112 (USB-HID bridge) → PCA9548 muxes (×5) → QSFP EEPROM at 0x50
```

Two earlier approaches both had critical defects:

**Phase 1 approach (i2c_mux_pca954x + optoe1):** Loading `i2c_mux_pca954x`
causes the kernel to probe address `0x50` on every mux channel at registration
time. QSFP EEPROMs at `0x50` are writable. The probe writes corrupted the installed
transceivers during early bring-up. Additionally, `optoe1` registered lazily
(first xcvrd read) meant DAC cable EEPROMs were not present in sysfs before
xcvrd's startup scan, causing `xcvrd` to cache type=GBIC.

**EOS-P1 approach (i2c-dev ioctl with mux drivers loaded):** Phase 1 daemon
used `i2c-dev` ioctl for PCA9535 reads. But `i2c_mux_pca954x` was still loaded
and its kernel driver generated concurrent mux-select writes on the same CP2112
while the daemon was mid-transfer. This caused hidraw to receive garbled
mux-state reads.

**Solution:** Remove `i2c_mux_pca954x`, `optoe`, and `at24` from the kernel
entirely. Write a daemon that owns `/dev/hidraw0` exclusively and navigates the
mux tree via raw CP2112 HID reports — matching how Arista EOS (`PLXcvrd`) handles
this platform.

## Proposed Approach

Write `utils/wedge100s-i2c-daemon.c`: a compiled C binary that:
1. Tries to open `/dev/hidraw0` for direct CP2112 access (Phase 2 path).
2. If successful: navigates PCA9548 muxes via raw HID reports, reads PCA9535
   presence, reads QSFP EEPROMs on insertion, reads system EEPROM once at boot.
3. If `/dev/hidraw0` unavailable: falls back to `i2c-dev` ioctl (Phase 1 path).
4. Writes output to `/run/wedge100s/`: `syseeprom`, `sfp_N_present` (0 or 1),
   `sfp_N_eeprom` (256 bytes binary) for occupied ports.

Invoked by `wedge100s-i2c-poller.timer` every 3 seconds (one-shot binary, not a
long-running daemon).

### CP2112 HID protocol

The CP2112 (Silicon Labs) supports standard SMBus operations via USB HID output
reports. Report IDs used:

| ID | Name | Direction | Purpose |
|----|------|-----------|---------|
| 0x14 | DATA_WRITE_REQUEST | host→CP2112 | Write N bytes to I2C addr |
| 0x11 | DATA_WRITE_READ_REQUEST | host→CP2112 | Write then read (repeated start) |
| 0x12 | DATA_READ_FORCE_SEND | host→CP2112 | Request N bytes from read buffer |
| 0x13 | DATA_READ_RESPONSE | CP2112→host | Received bytes |
| 0x15 | TRANSFER_STATUS_REQUEST | host→CP2112 | Poll transfer completion |
| 0x16 | TRANSFER_STATUS_RESPONSE | CP2112→host | Status: idle/busy/complete/error |
| 0x17 | CANCEL_TRANSFER | host→CP2112 | Abort pending transfer |

### Mux navigation

`bus_to_mux_addr(bus)` maps logical bus number to PCA9548 I2C address.
`bus_to_mux_channel(bus)` maps to channel number within that mux.
`mux_select(mux_addr, channel)` writes `1 << channel` to the mux (single byte).
`mux_deselect(mux_addr)` writes `0x00` to close all channels.

### Output files

| File | Size | Content |
|------|------|---------|
| `/run/wedge100s/syseeprom` | 8192 bytes | ONIE TLV binary (written once at boot) |
| `/run/wedge100s/sfp_N_present` | text | `"0"` or `"1"` for N=0..31 |
| `/run/wedge100s/sfp_N_eeprom` | 256 bytes | QSFP EEPROM page 0 (binary; present ports only) |

Total: 1 + 32 + up to 32 = up to 65 files.

### Files to Change

- `utils/wedge100s-i2c-daemon.c` — new file
- `service/wedge100s-i2c-poller.service` and `.timer` — new files
- `utils/accton_wedge100s_util.py` — remove mux/optoe/at24 from kos and mknod
- `sonic_platform/sfp.py` — primary path reads daemon cache
- `sonic_platform/chassis.py` — bulk presence reads from daemon files
- `sonic_platform/eeprom.py` — reads `/run/wedge100s/syseeprom`

## Acceptance Criteria

- `systemctl is-active wedge100s-i2c-poller.timer` = `active`
- `/run/wedge100s/syseeprom` exists and starts with `TlvInfo\x00`
- `/run/wedge100s/sfp_N_present` files exist for all N=0..31
- For every port where a module is physically installed: `sfp_N_present=1`
  and `sfp_N_eeprom` exists with byte 0 in range 0x01–0x7f
- `i2c_mux_pca954x` NOT loaded

## Risks and Watchpoints

**Exclusive hidraw ownership.** When `hid_cp2112` is loaded, it receives all
CP2112 interrupt reports (HID input reports). If the daemon reads hidraw while
`hid_cp2112` is servicing a kernel I2C transaction (e.g., CPLD read), the CP2112
will be mid-transfer. The CPLD is at address 0x32 (no mux); it never changes mux
state, so interleaving is safe. Do NOT add any mux-tree access from the kernel
(no `i2c_mux_pca954x`).

**Phase 1 → Phase 2 transition requires clean reboot.** Unbinding
`i2c_mux_pca954x` live while `gpio_pca953x` holds references to PCA9535 devices
causes a kernel hang (`i2c_del_adapter` blocks waiting for `i2c-dev` references
to release). Always reboot with the new `util.py` in place.

**EEPROM validity check.** Do not cache bytes 0x00 or 0x80–0xff as the
identifier byte (SFF-8024 range). These indicate an unresponsive or corrupt
EEPROM. The daemon retries every 3 s until a valid identifier is seen.

**Syseeprom: write once, never overwrite.** System EEPROM is static. The daemon
checks if `/run/wedge100s/syseeprom` already exists before reading hardware.
