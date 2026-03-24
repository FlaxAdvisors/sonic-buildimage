# SONiC Wedge 100S-32X — OpenBMC Communications Guide

**Date:** 2026-03-23
**Verified on hardware:** yes
**Branch:** `wedge100s`

---

## Overview

The Accton Wedge 100S-32X has a split management architecture: the host CPU (x86, running SONiC) and the OpenBMC (Aspeed AST2500) are separate processors sharing the board. Thermal sensors, fans, PSUs, and the syscpld LED control register are only accessible from the BMC side — they are not on the host's I2C bus. SONiC platform code must cross the host↔BMC boundary to read or write these resources.

This guide documents the two communication paths (TTY and SSH), the runtime architecture, the `/run/wedge100s/` file interface, and the constraints that rule out certain approaches.

---

## 1. Physical Link: USB CDC Composite Gadget

The BMC presents a **USB CDC Composite Gadget** (Netchip Technology, vendor:product `0525:a4aa`) to the host via an internal USB connection:

```
host USB controller
    └── BMC ast-vhub (Aspeed virtual hub)
            ├── CDC-ACM function  → /dev/ttyACM0  (serial console, 57600 8N1)
            └── CDC-ECM function  → usb0           (Ethernet-over-USB)
```

Both interfaces are enumerated when the host kernel loads the corresponding drivers (`cdc_acm`, `cdc_ether`). Both are present and functional on SONiC. Neither is available in ONIE (no kernel modules in the ONIE image).

### Interface addresses

| Side | Interface | MAC | IPv6 link-local |
|------|-----------|-----|-----------------|
| Host (SONiC) | `usb0` | `02:00:00:00:00:02` | `fe80::ff:fe00:2%usb0` |
| BMC | `usb0` | `02:00:00:00:00:01` | `fe80::ff:fe00:1%usb0` |

MACs are fixed (programmed into the BMC firmware), so the IPv6 link-local addresses are deterministic and need no DHCP or configuration. The host brings `usb0` up at platform init (`ip link set usb0 up`); IPv6 LL auto-configures from the MAC.

No IPv4 address is assigned to either end. All runtime SSH uses the IPv6 LL address.

---

## 2. Communication Paths

### 2.1 TTY — /dev/ttyACM0 (bootstrap only)

**Used for:** one-time SSH key provisioning at SONiC install/upgrade time.
**Not used for:** any runtime sensor polling or register writes (too slow, blocking).

Settings: 57600 8N1 raw, blocking I/O.
Login: `root` / `0penBmc`
Prompt pattern: `:~# ` (matches any OpenBMC hostname)

The `sonic-platform-accton-wedge100s-32x.postinst` uses the TTY to push the platform SSH key (`/etc/sonic/wedge100s-bmc-key`) to the BMC's `authorized_keys` and persist it to `/mnt/data/etc/authorized_keys` so it survives BMC reboots.

**Caution:** the BMC's `/etc/authorized_keys` is in a tmpfs-like location and is re-populated from `/mnt/data/etc/authorized_keys` at boot. If `/mnt/data` is cleared (factory reset, reflash), the key is lost and the TTY path must be used again to re-provision. The postinst re-runs the provisioning idempotently on every `.deb` upgrade.

### 2.2 SSH — root@fe80::ff:fe00:1%usb0 (runtime)

**Used for:** all runtime BMC access — sensor reads, GPIO reads, syscpld register writes.
**Key:** `/etc/sonic/wedge100s-bmc-key` (ed25519, root-owned 0600)
**Host key check:** `StrictHostKeyChecking=no` (BMC host key changes on reflash; known_hosts is not reliable)

The key is provisioned once via TTY (see §2.1). After that, all communication uses SSH:

```bash
# From SONiC host (as root):
ssh -o StrictHostKeyChecking=no -o BatchMode=yes \
    -i /etc/sonic/wedge100s-bmc-key \
    root@fe80::ff:fe00:1%usb0 'COMMAND'
```

SSH latency over usb0 is ~0.5 ms RTT (effectively loopback). Connection setup (handshake) is ~200–300 ms. The bmc-daemon uses SSH ControlMaster to pay this overhead only once per 10-second polling cycle.

---

## 3. Runtime Architecture: wedge100s-bmc-daemon

### 3.1 Design

`wedge100s-bmc-daemon` (C binary, `/usr/bin/wedge100s-bmc-daemon`) is a one-shot program invoked every 10 seconds by `wedge100s-bmc-poller.timer`. Each invocation:

1. Establishes an SSH ControlMaster session (`-f -N -o ControlMaster=yes`)
2. Processes any pending write-requests (`.set` files in `/run/wedge100s/`)
3. Reads all BMC-side sensors and GPIOs via multiplexed SSH commands
4. Writes results as plain decimal integers to `/run/wedge100s/`
5. Closes the ControlMaster

The ControlMaster pattern means only one SSH handshake per cycle regardless of how many individual commands are run. Each `bmc_run()`/`bmc_read_int()` call reuses the socket via `-o ControlMaster=no -o ControlPath=<socket>`.

### 3.2 Output Files

All files in `/run/wedge100s/` are plain decimal integers followed by a newline.

| File | BMC source command | Update rate | Description |
|------|--------------------|-------------|-------------|
| `thermal_{1..7}` | `cat /sys/bus/i2c/devices/3-004{8..c,8-0048,8-0049}/hwmon/*/temp1_input` | every 10 s | TMP75 temperature in millidegrees C |
| `fan_present` | `cat /sys/bus/i2c/devices/8-0033/fantray_present` | every 10 s | bitmask; 0 = all present |
| `fan_{1..5}_front` | `cat /sys/bus/i2c/devices/8-0033/fan{1,3,5,7,9}_input` | every 10 s | Front-rotor RPM |
| `fan_{1..5}_rear` | `cat /sys/bus/i2c/devices/8-0033/fan{2,4,6,8,10}_input` | every 10 s | Rear-rotor RPM |
| `psu_{1,2}_{vin,iin,iout,pout}` | `i2cget -f -y 7 0x59/0x5a 0x88/0x89/0x8c/0x96 w` | every 10 s | Raw PMBus LINEAR11 16-bit word |
| `syscpld_led_ctrl` | `i2cget -f -y 12 0x31 0x3c` | every 10 s | syscpld register 0x3c (LED control byte) |
| `qsfp_int` | `cat /sys/class/gpio/gpio31/value` | every 10 s | BMC AST GPIO31 (BMC_CPLD_QSFP_INT), active-low |
| `qsfp_led_position` | `cat /sys/class/gpio/gpio59/value` | once at boot | Board strap GPIOH3 (LED chain direction), value=1 |

Python platform APIs (thermal, fan, PSU) read these files instead of issuing direct TTY I/O.

### 3.3 Write-Request Pattern

Platform code that needs to write to a BMC-controlled register does so indirectly:

1. Write the desired value to `/run/wedge100s/<register>.set` (decimal or hex integer)
2. The bmc-daemon reads the `.set` file on its next 10-second cycle
3. Dispatches the write to the BMC via SSH
4. Removes the `.set` file (acknowledgement)

This keeps all BMC I/O centralized in the daemon — no platform Python module writes directly to the BMC.

**Current write-request:** `syscpld_led_ctrl.set`

```python
# From accton_wedge100s_util.py _request_led_init():
with open('/run/wedge100s/syscpld_led_ctrl.set', 'w') as f:
    f.write('0x02\n')
```

The daemon decodes the byte into individual sysfs attribute writes (see §4.3).

---

## 4. syscpld — Port LED Control

### 4.1 What it is

The **syscpld** (BMC i2c-12 / address 0x31) is a cross-domain CPLD that sits physically in the LED drive path between the BCM56960 LEDUP0/1 scan chain outputs and the physical QSFP port LED drivers. It is controlled entirely from the BMC side — the host CPU has no I2C path to it.

The syscpld kernel driver (bound on the BMC) exposes sysfs attributes at:
```
/sys/bus/i2c/devices/12-0031/
```

### 4.2 Register 0x3c — LED Control

ONIE sets this register to `0xe0` (rainbow animation, BCM LEDUP gated off). SONiC must write `0x02` to enable BCM LEDUP passthrough.

| Bit | Sysfs attribute | ONIE value | SONiC target | Meaning |
|-----|-----------------|-----------|--------------|---------|
| 7 | `led_test_mode_en` | 1 | **0** | CPLD-driven LED test mode |
| 6 | `led_test_blink_en` | 1 | **0** | Blinking in test mode |
| [5:4] | `th_led_steam` | 2 | **0** | Test pattern stream (2 = all-port cycle) |
| 3 | `walk_test_en` | 0 | 0 | Walk test |
| 1 | `th_led_en` | 0 | **1** | BCM LEDUP scan chain passthrough |
| 0 | `th_led_clr` | 0 | 0 | Clear LEDUP data |

- `0xe0` = test mode + blinking + all-LED stream cycling; BCM LEDUP **disabled** (rainbow)
- `0x02` = all test modes off; BCM LEDUP **enabled** (BCM controls port LEDs)

### 4.3 Why sysfs attributes, not i2cset

The syscpld driver owns i2c-12/0x31. Using `i2cset -f -y 12 0x31 0x3c 0x02` bypasses the driver's i2c bus lock and can corrupt a concurrent driver transaction (e.g., if `rest.py` or `ipmid` reads a power attribute at the same moment).

The daemon writes individual sysfs attributes instead, which go through the driver's serialised i2c access:

```bash
# What the daemon sends via SSH when syscpld_led_ctrl.set = 0x02:
echo 0 > /sys/bus/i2c/devices/12-0031/led_test_mode_en
echo 0 > /sys/bus/i2c/devices/12-0031/led_test_blink_en
echo 0 > /sys/bus/i2c/devices/12-0031/th_led_steam
echo 0 > /sys/bus/i2c/devices/12-0031/walk_test_en
echo 1 > /sys/bus/i2c/devices/12-0031/th_led_en
echo 0 > /sys/bus/i2c/devices/12-0031/th_led_clr
```

Reading `syscpld_led_ctrl` uses `i2cget -f -y 12 0x31 0x3c` (read-only, low risk) because there is no single sysfs attribute that exposes the full register byte.

### 4.4 BMC Daemons That Access i2c-12 (verified 2026-03-23)

| Daemon | Process | i2c-12 access | Frequency |
|--------|---------|---------------|-----------|
| `fscd.py` | Fan speed control | None (uses i2c-8) | N/A |
| `psumuxmon` | PSU mux monitor | None (uses i2c-7) | N/A |
| `ipmid` | IPMI daemon | Power attributes via sysfs | On-demand |
| `rest.py` | REST API | `board-utils.sh` power attrs | On-demand |
| `rest_usb2i2c_reset.py` | USB reset endpoint | `usb2cp2112_rst_n` only | On-demand |
| `kcsd` | KCS (IPMI) | Power control | On-demand |

No BMC daemon polls register 0x3c (LED control) at runtime. The only active periodic i2c-12 user is the SONiC bmc-daemon itself, via the sysfs layer.

### 4.5 LED Init Boot Sequence

```
SONiC boot
  └── wedge100s-platform-init.service
        └── accton_wedge100s_util.py install
              └── _request_led_init()
                    writes /run/wedge100s/syscpld_led_ctrl.set = 0x02

wedge100s-bmc-poller.timer (t ≈ 10 s)
  └── wedge100s-bmc-daemon
        ├── sees syscpld_led_ctrl.set
        ├── SSH to BMC: echo 0 > .../led_test_mode_en  (etc.)
        ├── removes .set file
        └── reads register back → syscpld_led_ctrl = 2
```

Rainbow clears within ~10–15 seconds of SONiC boot.

**ONIE limitation:** There is no path from ONIE to the BMC. ONIE has no cdc_acm or cdc_ether drivers (no loadable modules at all), so neither /dev/ttyACM0 nor usb0 is available. The management network may not be configured. The rainbow persists during ONIE install and the first ~15 s of SONiC boot — this is expected behaviour.

---

## 5. BMC GPIO Architecture

### 5.1 Named GPIOs Relevant to SONiC

Exported via `/sys/class/gpio/` and named via `/tmp/gpionames/` on the BMC.

| Shadow name | GPIO | BMC pin | Dir | Boot value | Role |
|-------------|------|---------|-----|------------|------|
| `BMC_CPLD_QSFP_INT` | gpio31 | GPIOD7 | in | 0 (asserted) | QSFP presence interrupt from syscpld |
| `QSFP_LED_POSITION` | gpio59 | GPIOH3 | in | 1 | Board strap: LED chain scan direction |
| `LED_PWR_BLUE` | gpio40 | GPIOE5 | out | — | Front-panel power LED (not port LEDs) |
| `PANTHER_I2C_ALERT_N` | gpio8 | GPIOB0 | in | — | BCM56960 I2C alert to BMC |
| `BMC_CPLD_POWER_INT` | gpio97 | GPIOQ4 | in | — | Power interrupt from syscpld |

### 5.2 BMC_CPLD_QSFP_INT (gpio31)

- **Active-low:** value 0 = interrupt asserted (QSFP inserted or removed)
- **Cleared by:** reading the PCA9535 INPUT register (which `wedge100s-i2c-daemon` does every 3 s via hidraw)
- **Exposed as:** `/run/wedge100s/qsfp_int` (updated every 10 s by bmc-daemon)
- **Future use:** i2c-daemon can watch this file for a 0→0 transition to trigger an immediate presence scan, reducing insertion detection latency from up to 3 s to near-instant

### 5.3 QSFP_LED_POSITION (gpio59)

Board strap read at PCB assembly time; value = **1** on this hardware. Indicates the physical orientation of the QSFP LED scan chains relative to the front panel port numbering. Relevant to the BCM LEDUP port-order remap table in `led_proc_init.soc`.

Exposed as `/run/wedge100s/qsfp_led_position` (written once at first daemon run after boot; not refreshed if file exists).

---

## 6. BMC Recovery Mechanisms

The syscpld on i2c-12 also controls host-side I2C recovery, accessible from the BMC:

| Attribute path | Mechanism | Script |
|----------------|-----------|--------|
| `12-0031/i2c_flush_en` | Pulse CP2112 I2C flush (1→0) | `/usr/local/bin/cp2112_i2c_flush.sh` |
| `12-0031/usb2cp2112_rst_n` | Hard-reset CP2112 USB bridge (write 0) | `rest_usb2i2c_reset.py` REST endpoint |
| `12-0031/i2c_mux{0..3}_rst_n` | Reset QSFP PCA9548 mux (write 0) | `/usr/local/bin/reset_qsfp_mux.sh` |

These can be invoked from SONiC via:
```bash
ssh -i /etc/sonic/wedge100s-bmc-key root@fe80::ff:fe00:1%usb0 \
    '/usr/local/bin/cp2112_i2c_flush.sh'
```

Use when the host sees `ast-i2c: recovery error` or `I2C(10) reset completed` in dmesg, indicating a CP2112 bus hang.

---

## 7. Key File Paths

| File | Location | Description |
|------|----------|-------------|
| BMC SSH key | `/etc/sonic/wedge100s-bmc-key` | ed25519 private key, root:root 0600 |
| BMC SSH pubkey | `/etc/sonic/wedge100s-bmc-key.pub` | Provisioned to BMC authorized_keys |
| ControlMaster socket | `/run/wedge100s/.bmc-ctl` | Ephemeral; exists only during daemon run |
| bmc-daemon source | `platform/.../utils/wedge100s-bmc-daemon.c` | SSH-based, ~330 lines |
| bmc-daemon binary | `/usr/bin/wedge100s-bmc-daemon` | Installed by .deb |
| Timer unit | `wedge100s-bmc-poller.timer` | OnCalendar=*:*:0/10 (every 10 s) |
| Service unit | `wedge100s-bmc-poller.service` | Type=oneshot, invokes daemon |
| LED init request | `/run/wedge100s/syscpld_led_ctrl.set` | Written by platform init, consumed by daemon |
| Run directory | `/run/wedge100s/` | All daemon output files; tmpfs, recreated each boot |

---

## 8. Operational Notes

**After a BMC reboot:** `authorized_keys` on the BMC is restored from `/mnt/data/etc/authorized_keys` at boot. If the BMC's `/mnt/data` was wiped, the key is gone and the bmc-daemon will fail silently (SSH auth failure). Re-provision via TTY:

```bash
# From SONiC host:
python3 -c "from sonic_platform import bmc; bmc.provision_ssh_key()"
# Or manually:
ssh-copy-id -i /etc/sonic/wedge100s-bmc-key root@fe80::ff:fe00:1%usb0
# Password: 0penBmc
```

**Testing the daemon manually:**

```bash
sudo systemctl stop wedge100s-bmc-poller.timer
sudo /usr/bin/wedge100s-bmc-daemon
# Check results:
cat /run/wedge100s/syscpld_led_ctrl   # should be 2 (0x02)
cat /run/wedge100s/thermal_1          # mC
cat /run/wedge100s/qsfp_int           # 0 if QSFPs present
sudo systemctl start wedge100s-bmc-poller.timer
```

**Forcing LED init (clear rainbow manually):**

```bash
echo 0x02 | sudo tee /run/wedge100s/syscpld_led_ctrl.set
# daemon picks it up within 10 s; or run manually:
sudo /usr/bin/wedge100s-bmc-daemon
```

**Direct BMC register inspection:**

```bash
# From SONiC:
ssh -i /etc/sonic/wedge100s-bmc-key root@fe80::ff:fe00:1%usb0 \
    'i2cget -f -y 12 0x31 0x3c'
# Expected: 0x02 (normal SONiC state)
# Bad:      0xe0 (ONIE left rainbow mode)

# Read LED attributes directly:
ssh -i /etc/sonic/wedge100s-bmc-key root@fe80::ff:fe00:1%usb0 \
    'cat /sys/bus/i2c/devices/12-0031/led_test_mode_en \
         /sys/bus/i2c/devices/12-0031/th_led_en'
# Expected: 0x0 (test mode off) / 0x1 (BCM LEDUP enabled)
```
