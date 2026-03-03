# Hardware Workflow — Wedge 100S-32X

## Target Inventory

| Target | Address | Access | Notes |
|---|---|---|---|
| SONiC switch | `192.168.88.12` | `ssh admin@192.168.88.12` | Primary development target (hare-lorax) |
| OpenBMC | `192.168.88.13` | `ssh root@192.168.88.13` | Environmental controller (thermal, fan, PSU) |
| BMC TTY | Local host | `/dev/ttyACM0` at 57600 baud | Fallback when SSH is unavailable |

---

## BMC SSH — After Every Reboot

The BMC clears `~/.ssh/authorized_keys` on every reboot. After a BMC reboot:

- `ping 192.168.88.13` → succeeds (BMC is up)
- `ssh root@192.168.88.13` → "Connection refused" or "Permission denied"

### Detection

```bash
ping -c1 -W2 192.168.88.13 && ssh -o ConnectTimeout=5 -o BatchMode=yes root@192.168.88.13 echo ok
```

If ping succeeds and SSH fails → BMC has rebooted.

### Fix: Re-install your SSH key

```bash
ssh-copy-id root@192.168.88.13
# Password: 0penBmc
```

Verify:

```bash
ssh root@192.168.88.13 echo "key works"
```

### CLAUDE.md Integration

The project CLAUDE.md instructs Claude to detect this condition and prompt you before
attempting any BMC SSH operations. You should not need to diagnose it manually during
a Claude-assisted session — Claude will stop and tell you.

---

## BMC TTY Fallback

When SSH to the BMC is down and you cannot run `ssh-copy-id` (e.g. no password auth
configured), use the USB CDC serial interface:

```bash
# Connect (blocking, VMIN=1 — required; O_NONBLOCK does NOT work)
screen /dev/ttyACM0 57600

# Or with minicom
minicom -D /dev/ttyACM0 -b 57600

# Login: root / 0penBmc
```

Note: BMC shell prompt is `root@<hostname>:~#` where hostname varies by unit.
The bmc.py helper matches on `b':~# '` (any hostname).

---

## Test Suite

### Setup

Edit `tests/target.cfg` with the live switch details:

```ini
[target]
host = 192.168.88.12
port = 22
username = admin
key_file = ~/.ssh/id_rsa
```

`target.cfg` is gitignored. Copy from `target.cfg.example` if missing.

### Running Tests

```bash
cd tests

# All stages
python run_tests.py

# Single stage (verbose)
pytest stage_01_eeprom/ -v
pytest stage_04_thermal/ -v
pytest stage_05_fan/ -v

# With output capture off (see print statements live)
pytest stage_03_i2c_bmc/ -v -s
```

### Test Stage Map

| Stage | Directory | Tests |
|---|---|---|
| 01 | `stage_01_eeprom/` | System EEPROM TLV fields |
| 02 | `stage_02_system/` | Platform init, device registration |
| 03 | `stage_03_i2c_bmc/` | I2C bus topology, BMC TTY |
| 04 | `stage_04_thermal/` | Thermal sensors (CPU coretemp + 7x TMP75) |
| 05 | `stage_05_fan/` | Fan trays (5x, RPM and presence) |
| 06 | `stage_06_psu/` | PSU presence (CPLD) + telemetry (BMC PMBus) |
| 07 | `stage_07_qsfp/` | QSFP28 presence, EEPROM, port mapping |
| 08 | `stage_08_led/` | System LED state via CPLD |

---

## Safe pmon Restart

The `pmon` container runs xcvrd (QSFP I2C transactions), thermalctld, and ledd.
Killing it while xcvrd is mid-transaction hangs the SDA line (requires power cycle).

```bash
# CORRECT — graceful
sudo systemctl stop pmon
sudo systemctl start pmon

# Check status
sudo systemctl status pmon
docker exec pmon supervisorctl status
```

**NEVER:**

```bash
docker rm -f pmon   # ← hangs I2C bus if xcvrd was active
```

Recovery after I2C bus hang: OpenBMC `wedge_power.sh reset` (hard power cycle).

---

## Deploying Platform Updates

```bash
# On build host — build the .deb
make target/debs/sonic-platform-accton-wedge100s-32x_1.0_amd64.deb

# Copy to switch
scp target/debs/sonic-platform-accton-wedge100s-32x_1.0_amd64.deb admin@192.168.88.12:~

# On switch — install
sudo systemctl stop pmon
sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.0_amd64.deb
sudo systemctl start pmon
```

The `.postinst` script:
- Runs `depmod -a`
- Enables and starts `wedge100s-platform-init.service`
- Patches `/usr/bin/pmon.sh` to add `/dev/ttyACM0` device mount (idempotent)

---

## I2C Quick Reference

```bash
# List all I2C buses (on switch)
i2cdetect -l

# Scan a bus
i2cdetect -y 1          # CP2112 (main bus)
i2cdetect -y 40         # mux channel (COME cluster)

# Read a register
i2cget -f -y 1 0x32 0x10    # CPLD PSU status

# Write a register
i2cset -f -y 1 0x32 0x3e 0x02   # CPLD SYS1 LED = green

# Register system EEPROM (if not auto-registered)
echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device
hexdump -C /sys/bus/i2c/devices/40-0051/eeprom
```

---

## Useful One-liners on the Switch

```bash
# Check pmon container devices
docker exec pmon ls /dev/i2c-* /dev/ttyACM*

# Check thermalctld is running
docker exec pmon supervisorctl status thermalctld

# Read all thermal sensors
for f in /sys/bus/i2c/devices/{3-0048,3-0049,3-004a,3-004b,3-004c,8-0048,8-0049}/hwmon/*/temp1_input; do
  echo "$f: $(cat $f)"
done

# Fan RPM (BMC)
for i in $(seq 1 10); do
  echo "fan${i}: $(cat /sys/bus/i2c/devices/8-0033/fan${i}_input)"
done

# Force write CONFIG_DB (required before ledd works)
sudo sonic-cfggen -H -k Accton-WEDGE100S-32X --write-to-db
```
