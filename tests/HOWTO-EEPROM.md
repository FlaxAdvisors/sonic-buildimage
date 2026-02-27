# HOWTO: I2C, Muxes, and EEPROM on the Accton Wedge 100S-32X

This guide explains the I2C bus system, multiplexers, EEPROM chip types, and how to read
and write EEPROMs from Linux — using the Wedge 100S-32X as a concrete example.

---

## 1. I2C — What It Is and How It Works

**I2C** (Inter-Integrated Circuit, pronounced "I-squared-C") is a two-wire serial bus
invented by Philips in the 1980s.  Every board in a modern network switch uses it
to connect low-speed peripheral chips: EEPROMs, temperature sensors, GPIO expanders,
power monitors, fan controllers, LED drivers, and more.

### The two wires

| Signal | Full Name        | Role |
|--------|-----------------|------|
| SDA    | Serial Data     | Bidirectional data line |
| SCL    | Serial Clock    | Master-driven clock |

Both lines are **open-drain** with pull-up resistors.  Any device on the bus can pull a
line low; the resistor pulls it high when no one is driving it.  This is how multiple
devices share one pair of wires without short-circuits.

### Addresses

Every device on an I2C bus has a **7-bit address** (0x00–0x7F).  The master (the CPU)
initiates every transaction; slaves only respond.  A transaction looks like this:

```
START | ADDR[6:0] R/W̄ | ACK | DATA byte(s) | ACK | … | STOP
```

- **START**: SDA goes low while SCL is high — the bus "wakes up"
- **Address + direction bit**: 7 address bits + 1 read/write bit, sent MSB-first
- **ACK**: the addressed slave pulls SDA low to say "I'm here, go ahead"
- **STOP**: SDA goes high while SCL is high — bus released

If no device acknowledges (NACK), the master gets a protocol error.  This is how
`i2cdetect` finds which addresses are populated: it sends a minimal probe to every
possible address and watches for ACKs.

### Standard speeds

| Mode        | Clock     | Typical use |
|-------------|-----------|-------------|
| Standard    | 100 kHz   | Slow sensors, EEPROMs |
| Fast        | 400 kHz   | Most peripherals |
| Fast+       | 1 MHz     | Modern SoCs |

The Wedge 100S-32X CP2112 bridge runs at 100 kHz by default for the host I2C tree.

---

## 2. The I2C Bus on This Platform

On x86 servers you normally see an **iSMT** or **I801** SMBus controller.  The Wedge
100S-32X uses a **CP2112 USB HID I2C bridge** (Silicon Labs, USB VID/PID 10c4:ea90)
as the host I2C master.  It appears as `/dev/i2c-1` under Linux.

```
x86 Host CPU
    │
    └── USB 2.0
            │
        [CP2112 bridge]    ← /dev/i2c-1
            │
            └── I2C bus (SDA/SCL pair)
                    │
                    ├── CPLD  @ 0x32
                    ├── COME module EC  @ 0x50
                    ├── COME module EEPROM  @ 0x51
                    └── 5× PCA9548 mux  @ 0x70–0x74
                               └── (QSFP EEPROMs, PCA9535 GPIO, ...)
```

The CP2112 wraps every I2C transaction inside a **USB HID report**.  Each report is an
independent USB transaction — there is no way for the driver to atomically chain two
reports together.  This has important consequences for mux usage (Section 3).

---

## 3. I2C Multiplexers

32 QSFP ports each need an EEPROM at address 0x50.  You cannot put 32 chips at the same
address on one bus — they would all respond simultaneously and corrupt each other's data.

The solution is an **I2C multiplexer (mux)**, specifically the **PCA9548** from NXP.
It has one upstream port (connected to the master) and eight downstream channels.
Only one channel is active at a time, selected by writing a control byte to the mux's
own I2C address.

```
Control byte: bit N = 1  →  enable channel N
              0x00        →  all channels disabled
              0x01        →  channel 0 only
              0x40        →  channel 6 only
              0x80        →  channel 7 only
```

The Wedge 100S-32X has **five PCA9548 muxes** at addresses 0x70–0x74 on i2c-1.  The
Linux kernel's `i2c_mux_pca954x` driver creates a virtual I2C bus for each channel:

```
i2c-1 (CP2112 root)
 ├── mux 0x70  →  channels 0-7  →  buses i2c-2 … i2c-9
 ├── mux 0x71  →  channels 0-7  →  buses i2c-10 … i2c-17
 ├── mux 0x72  →  channels 0-7  →  buses i2c-18 … i2c-25
 ├── mux 0x73  →  channels 0-7  →  buses i2c-26 … i2c-33
 └── mux 0x74  →  channels 0-7  →  buses i2c-34 … i2c-41
                     channel 2  →  i2c-36  (PCA9535 QSFP presence 0-15)
                     channel 3  →  i2c-37  (PCA9535 QSFP presence 16-31)
                     channel 6  →  i2c-40  (system EEPROM registration bus)
```

### How the kernel uses a mux

When your code opens `/sys/bus/i2c/devices/40-0051/eeprom`, the kernel executes:

1. Lock the I2C bus adapter (i2c-1)
2. Write `0x40` to mux 0x74 (select channel 6)
3. Send the EEPROM read to address 0x51
4. Write `0x00` to mux 0x74 (deselect all — if `force_deselect_on_exit=1`)
5. Unlock the adapter

### The CP2112 atomicity problem

On a normal iSMT controller, steps 1–5 happen atomically from the hardware's perspective.
The CP2112 is different: **step 2 is one USB HID report and step 3 is a separate USB HID
report**.  Between those two USB transactions, another kernel thread, userspace process,
or `pmon` daemon can issue its own USB HID report to the CP2112, changing the mux to a
different channel.  When step 3 executes, it hits the wrong device.

**Observed consequence on this platform:** When `xcvrd` polls the PCA9535 GPIO expanders
(channels 2 and 3) concurrently with a EEPROM write on channel 6, the mux state flips
between transactions.  Writes to `40-0050` that appeared to succeed (the device ACKed)
actually landed on whatever was currently selected — often a QSFP EEPROM or the COME EC
chip.

**Mitigation in production code:** `accton_wedge100s_util.py` caches the EEPROM contents
to `/var/run/platform_cache/syseeprom_cache` *before* `xcvrd`/`pmon` start, during the
narrow window when only the platform-init service is running and the bus is quiet.
At runtime, `sonic_platform/eeprom.py` reads from the cache, never from hardware.

**Rule of thumb:** If you need to write to an I2C device behind a PCA9548 on a CP2112
bus, stop all other I2C users first.

---

## 4. EEPROM Chip Types

### 4.1 AT24Cxx — The Standard Byte-Addressable EEPROM

The AT24 family (Microchip, formerly Atmel) is the most common I2C EEPROM on network
hardware.  Key variants:

| Part    | Size   | Address width | Page size | Vcc  |
|---------|--------|---------------|-----------|------|
| AT24C02 | 256 B  | 1 byte        | 8 B       | 1.7–5.5 V |
| AT24C64 | 8 KB   | 2 bytes       | 32 B      | 1.7–5.5 V |
| AT24C512| 64 KB  | 2 bytes       | 128 B     | 1.7–5.5 V |

**Address width** is critical.  To read byte at offset `N` you first send the offset as
a "register pointer" write:

```
AT24C02  (1-byte addr):   WRITE [chip_addr, N]          then READ
AT24C64  (2-byte addr):   WRITE [chip_addr, N>>8, N&0xFF]  then READ
```

If you register an AT24C64 (2-byte addressing) and the physical chip is actually something
else with 1-byte registers, the high byte of the address becomes unintended *data* written
to the chip's first register on every read attempt — this is exactly what corrupted the
COME EC chip at 0x50 on this board.

**Page writes:** AT24 EEPROMs write in pages.  You can send up to N bytes in one I2C
write transaction, where N is the page size.  Bytes beyond the page boundary wrap around
within the page (they do NOT overflow to the next page).  The chip takes 5–10 ms to
internally program the page after the I2C STOP; during this time it ignores all I2C
traffic (or NACKs, depending on the part).

**Write protect:** Many AT24 chips have a WP pin.  When WP=HIGH, the chip ACKs every
write at the I2C protocol level (the address byte gets an ACK) but silently discards the
data.  If you see "write OK" from `i2ctransfer` but readback shows the old value, check
the WP pin or a WP control register in your platform's CPLD.

### 4.2 The COME Module EC Chip at 0x50

The COME module (model B634) on this platform has an Embedded Controller (EC) that
exposes a read-only I2C register file at address 0x50.  It is **not** a standard AT24
EEPROM:

- Reads return ODM-format platform identity data (product name, firmware version, default MAC)
- Writes are ACKed at the I2C protocol level but the data is not stored
- The register layout uses 1-byte addressing (register index = first byte of write)
- Writing to this chip with the at24 2-byte-address protocol sends the address-high-byte
  as data to EC register 0, corrupting it

**Factory content (before our porting work):**

```
reg 0x00: 0x10   (firmware major version)
reg 0x01: 0x04   (firmware minor version)
reg 0x02: 0x06   (sub-version or board ID)
reg 0x03+: 0x00  (reserved)
```

This device appears on **every** channel of mux 0x74 (buses 34–41) because the CP2112
cannot hold the mux selection, so all those virtual buses are effectively transparent to
i2c-1.  It also appears on i2c-1 directly.

### 4.3 The System EEPROM at 0x51

An AT24C02 (256 bytes, 1-byte addressing, 8-byte pages) physically on i2c-1.  This is
where per-unit data lives:

- ONIE TlvInfo format
- Serial number, MAC address, manufacture date, etc.
- Registered in Linux via `echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device`
  (bus 40 is used for registration even though the chip is physically on i2c-1, because
   bus 40 is transparent to i2c-1 for COME module devices)

---

## 5. Linux I2C Tooling

### 5.1 i2cdetect — Scan a Bus for Devices

```bash
# Scan bus 1, using read probes (safe for most devices)
i2cdetect -y 1

# -y = don't prompt for confirmation
# Output: grid of addresses 0x00-0x7F
#   --  = no response
#   UU  = kernel driver already bound to this address
#   XX  = device responded (XX = hex address)
```

Example output from i2c-1 on the Wedge 100S-32X:

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:
10:
20:
30: -- -- 32 -- -- -- -- --
40:
50: UU 51 52 53 -- -- -- -- 58
60:
70: 70 71 72 73 74
```

`UU` at 0x50 means the at24 driver is bound to that address (or another driver on a child
bus has it claimed).  `51`, `52`, `53` respond but have no driver.  `70`–`74` are the
five PCA9548 muxes.

> **Warning:** `i2cdetect` uses SMBus Quick Write by default, which can trigger write
> operations on some devices (latched interrupts, write-only registers).  The `-r` flag
> uses read probes instead.  On buses where Quick Write is not supported (as with CP2112),
> the tool falls back to read probes automatically.

### 5.2 i2cget — Read One Byte

```bash
# Read one byte from register 0x10 at address 0x32 on bus 1
i2cget -f -y 1 0x32 0x10

# -f = force access even if a driver is bound
# -y = don't prompt
# Output: 0xe8
```

`i2cget` uses SMBus byte-data read: it sends the register address then reads one byte.
This is equivalent to AT24C02's 1-byte random-read.  It does NOT work for AT24C64-style
2-byte addressing.

### 5.3 i2cset — Write One Byte

```bash
# Write 0x02 to register 0x3e at address 0x32 on bus 1 (turn SYS1 LED green)
i2cset -f -y 1 0x32 0x3e 0x02
```

### 5.4 i2ctransfer — Arbitrary I2C Transactions

`i2ctransfer` gives you raw control over I2C messages.  Format:

```
i2ctransfer [-f] -y <bus> <msg> [<msg> ...]

<msg> = w<len>@<addr> <byte> [<byte>...]   (write)
      = r<len>@<addr>                       (read)
```

**Random read from AT24C02 (1-byte address) at 0x51 on bus 40:**

```bash
# Set address pointer to 0x00, then read 16 bytes
i2ctransfer -f -y 40 w1@0x51 0x00 r16@0x51
```

**Random read from AT24C64 (2-byte address) at 0x50 on bus 40:**

```bash
# Address = 0x0000, read 16 bytes
i2ctransfer -f -y 40 w2@0x50 0x00 0x00 r16@0x50
```

**Write 3 bytes starting at address 0x05 in AT24C02:**

```bash
# [addr=0x05, data=0xAA, 0xBB, 0xCC] — one write transaction
i2ctransfer -f -y 40 w4@0x51 0x05 0xAA 0xBB 0xCC
sleep 0.01  # wait for internal write cycle (AT24C02: 5 ms max)
```

> **Important:** `i2ctransfer` returning `OK` only means the device acknowledged at the
> I2C protocol level.  Always read back to confirm data was actually stored.

### 5.5 Direct Python Access via /dev/i2c-N

For more control — especially when you need to probe without a kernel driver bound, or
use raw reads/writes that bypass the at24 driver's addressing logic:

```python
import fcntl, time

I2C_SLAVE_FORCE = 0x0706   # ioctl to set slave address (force past bound drivers)

with open("/dev/i2c-1", "r+b", buffering=0) as f:
    # Point to device at 0x51
    fcntl.ioctl(f, I2C_SLAVE_FORCE, 0x51)

    # 1-byte-addressing read: send register 0x00, then read 8 bytes
    f.write(bytes([0x00]))     # sets register pointer
    data = f.read(8)
    print(data.hex())

    # 1-byte-addressing write: [register, data_byte]
    f.write(bytes([0x05, 0xAB]))   # write 0xAB to register 0x05
    time.sleep(0.01)               # AT24C02 write cycle
```

> `I2C_RDWR` ioctl (`0x0707`) is needed for combined write+read (restart without STOP).
> The simpler sequential write-then-read shown above works for AT24 EEPROMs because
> the chip latches the address pointer and holds it for a subsequent read transaction.

---

## 6. The at24 Kernel Driver and sysfs

When you register a device on an I2C bus, the kernel binds the appropriate driver and
creates sysfs nodes.  For AT24 EEPROMs the driver is `at24` and the key sysfs file is:

```
/sys/bus/i2c/devices/<bus>-<addr>/eeprom
```

For example, after `echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device`:

```
/sys/bus/i2c/devices/40-0051/eeprom   ← binary file, 256 bytes
```

### Reading with cat / xxd / Python

```bash
# Hex dump of the whole EEPROM
xxd /sys/bus/i2c/devices/40-0051/eeprom | head -12

# First 11 bytes (TlvInfo header)
python3 -c "
f = open('/sys/bus/i2c/devices/40-0051/eeprom', 'rb')
print(f.read(11).hex())
f.close()
"
```

### Writing via sysfs

The sysfs eeprom file must be opened in `r+b` mode (NOT `wb` — that truncates and breaks
the kernel's understanding of the file position).  For AT24C02 (small, 8-byte pages),
byte-by-byte writes with explicit seeks work reliably:

```python
def write_eeprom(path, data):
    with open(path, "r+b") as f:
        for i, byte in enumerate(data):
            f.seek(i)
            f.write(bytes([byte]))
            f.flush()
            # Allow write cycle: AT24C02 needs up to 5 ms per page (8 bytes)
            # Byte-at-a-time is slow but safe across page boundaries
            import time; time.sleep(0.006)

    # Verify
    with open(path, "rb") as f:
        readback = f.read(len(data))

    mismatches = [(i, data[i], readback[i])
                  for i in range(len(data)) if data[i] != readback[i]]
    return mismatches  # empty list = success
```

### Registering and unregistering devices

```bash
# Register AT24C02 at address 0x51 on bus 40
echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device

# Register AT24C64 at address 0x50 on bus 40
echo 24c64 0x50 > /sys/bus/i2c/devices/i2c-40/new_device

# Remove a device
echo 0x51 > /sys/bus/i2c/devices/i2c-40/delete_device

# Temporarily unbind the at24 driver (device stays registered, driver detaches)
echo 40-0051 > /sys/bus/i2c/drivers/at24/unbind

# Rebind
echo 40-0051 > /sys/bus/i2c/drivers/at24/bind
```

---

## 7. The ONIE TlvInfo Binary Format

ONIE (Open Network Install Environment) defines a standard binary format for system
EEPROM, called **TlvInfo**.  It is a simple tag-length-value encoding.

### 7.1 Header (11 bytes)

```
Offset  Len  Field         Value
------  ---  -----         -----
0       8    Magic         "TlvInfo\x00"  (0x54 6c 76 49 6e 66 6f 00)
8       1    Version       0x01
9-10    2    Total length  Big-endian uint16; total bytes of all TLV entries following
```

### 7.2 TLV Entries (variable)

Each entry immediately follows the previous one:

```
Offset  Len  Field    Notes
------  ---  -----    -----
+0      1    Type     One byte type code (see table below)
+1      1    Length   Number of value bytes (0–255)
+2      N    Value    Depends on type
```

**Common type codes:**

| Code | Name              | Value encoding |
|------|-------------------|----------------|
| 0x21 | Product Name      | ASCII string |
| 0x22 | Part Number       | ASCII string |
| 0x23 | Serial Number     | ASCII string |
| 0x24 | Base MAC Address  | 6 bytes, big-endian |
| 0x25 | Manufacture Date  | ASCII "MM/DD/YYYY HH:MM:SS" |
| 0x26 | Device Version    | 1 byte unsigned |
| 0x27 | Label Revision    | ASCII string |
| 0x28 | Platform Name     | ASCII string |
| 0x29 | ONIE Version      | ASCII string |
| 0x2A | MAC Count         | 2 bytes, big-endian uint16 |
| 0x2B | Manufacturer      | ASCII string |
| 0x2C | Country Code      | ASCII 2-char ISO 3166-1 |
| 0x2D | Vendor Name       | ASCII string |
| 0x2F | Service Tag       | ASCII string |
| 0xFE | CRC-32            | 4 bytes, big-endian; covers header + all prior TLVs |

The **CRC-32** entry MUST be the last one.  It uses the standard Ethernet CRC-32
polynomial (the same used by zlib/Python `binascii.crc32`).  The checksum covers all
bytes from the start of the magic through the end of the CRC entry's Length field,
with the CRC value itself initialised to 0 during calculation.

### 7.3 Parsing TlvInfo in Python

```python
import struct, binascii

MAGIC = b"TlvInfo\x00"

def parse_tlvinfo(data):
    if data[:8] != MAGIC:
        raise ValueError(f"Bad magic: {data[:8].hex()}")
    version = data[8]
    total_len = struct.unpack(">H", data[9:11])[0]
    print(f"TlvInfo v{version}, {total_len} bytes of TLV data")

    idx = 11   # first TLV entry starts right after the header
    end = 11 + total_len
    entries = {}

    while idx + 2 <= len(data) and idx < end:
        typ = data[idx]
        length = data[idx + 1]
        value = data[idx + 2 : idx + 2 + length]

        if typ == 0x24:   # MAC address: 6 raw bytes
            val_str = ":".join(f"{b:02X}" for b in value)
        elif typ == 0x26:  # Device version: 1 byte
            val_str = str(value[0])
        elif typ == 0x2A:  # MAC count: big-endian uint16
            val_str = str(struct.unpack(">H", value)[0])
        elif typ == 0xFE:  # CRC-32: big-endian uint32
            val_str = f"0x{struct.unpack('>I', value)[0]:08X}"
        else:
            val_str = value.decode("ascii", errors="replace")

        entries[typ] = val_str
        print(f"  0x{typ:02X}: {val_str}")

        if typ == 0xFE:
            break
        idx += 2 + length

    return entries

# Usage
with open("/sys/bus/i2c/devices/40-0051/eeprom", "rb") as f:
    raw = f.read(256)
parse_tlvinfo(raw)
```

### 7.4 Encoding TlvInfo with SONiC's TlvInfoDecoder

SONiC ships a utility class that handles encoding and decoding:

```python
from sonic_platform_base.sonic_eeprom.eeprom_tlvinfo import TlvInfoDecoder

# The path argument is used for reads; '' means encode-only
e = TlvInfoDecoder("/sys/bus/i2c/devices/40-0051/eeprom", 0, "", False)

# Build a new TlvInfo blob from scratch
new_data = e.set_eeprom(b"", [
    "0x21=WEDGE100S12V",          # Product Name
    "0x22=20-001688",             # Part Number
    "0x23=AI09019591",            # Serial Number
    "0x24=00:90:FB:61:DA:A1",     # Base MAC Address
    "0x25=03/09/2018 00:00:00",   # Manufacture Date
    "0x26=1",                     # Device Version
    "0x27=R01",                   # Label Revision
    "0x28=x86_64-accton_wedge100s_32x-r0",  # Platform Name
    "0x29=master-11171931-dirty", # ONIE Version
    "0x2A=129",                   # MAC Count
    "0x2B=Joytech",               # Manufacturer
    "0x2C=CN",                    # Country Code
    "0x2D=Accton",                # Vendor Name
    "0x2F=4358633",               # Service Tag
])

print(f"Encoded {len(new_data)} bytes")
print("First 11 bytes (header):", new_data[:11].hex())

# Write to EEPROM
e.write_eeprom(new_data)

# Verify with decode
e2 = TlvInfoDecoder("/sys/bus/i2c/devices/40-0051/eeprom", 0, "", False)
e2.decode_eeprom(e2.read_eeprom())
```

### 7.5 Inspecting a raw EEPROM binary with xxd

```bash
# Hex + ASCII dump of the whole EEPROM
xxd /sys/bus/i2c/devices/40-0051/eeprom

# Just the first 32 bytes (header + first couple of TLVs)
python3 -c "
import sys
data = open('/sys/bus/i2c/devices/40-0051/eeprom','rb').read(32)
for i in range(0, len(data), 16):
    row = data[i:i+16]
    hex_part = ' '.join(f'{b:02x}' for b in row)
    asc_part = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
    print(f'{i:04x}:  {hex_part:<48}  {asc_part}')
"
```

Example output for a valid TlvInfo EEPROM:

```
0000:  54 6c 76 49 6e 66 6f 00  01 00 a9 21 0c 57 45 44  TlvInfo....!.WED
0010:  47 45 31 30 30 53 31 32  56 22 09 32 30 2d 30 30  GE100S12V".20-00
```

Breakdown:
- `54 6c 76 49 6e 66 6f 00` = "TlvInfo\0" magic
- `01` = version 1
- `00 a9` = 169 bytes of TLV data follow
- `21 0c 57 45 44 47 45 31 30 30 53 31 32 56` = type=0x21, len=12, "WEDGE100S12V"
- `22 09 32 30 2d 30 30 31 36 38 38` = type=0x22, len=9, "20-001688"

---

## 8. Preventing I2C Bus Contention

### 8.1 Understanding the problem

On this platform, three classes of I2C traffic share i2c-1 simultaneously:

1. **QSFP EEPROM polls** (`xcvrd`): reads optoe EEPROMs on buses 2–33 every ~1 second
2. **QSFP presence polls** (`xcvrd`): reads PCA9535 on buses 36–37 every ~1 second
3. **System EEPROM reads**: one-time at startup; cached after that

Because the CP2112 cannot atomically chain mux-select + data transactions, any two of
these happening simultaneously can cause a mux channel switch between the select and
the data byte, landing the data on the wrong device.

### 8.2 The cache strategy

`accton_wedge100s_util.py` runs as a systemd service *before* `pmon` starts.  During
that window — with only one I2C user active — it reads the EEPROM and writes the raw
bytes to `/var/run/platform_cache/syseeprom_cache`.  From that point on, all of SONiC
reads from the cache file instead of hardware:

```python
# sonic_platform/eeprom.py
class SysEeprom(eeprom_tlvinfo.TlvInfoDecoder):
    EEPROM_PATH = "/sys/bus/i2c/devices/40-0051/eeprom"

    def __init__(self):
        # The third argument is the cache path; fourth=True means prefer cache
        super().__init__(self.EEPROM_PATH, 0, EEPROM_CACHE_PATH, True)
```

### 8.3 When you need to write to hardware during a running system

If you must write to an I2C device while the system is running:

```bash
# Step 1: Stop the processes that poll the bus
docker exec pmon supervisorctl stop xcvrd ledd

# Step 2: Do your I2C work
# ... write to EEPROM, configure GPIO, etc. ...

# Step 3: Restart
docker exec pmon supervisorctl start xcvrd ledd
```

For the system EEPROM specifically, also invalidate the cache so it is refreshed:

```bash
rm /var/run/platform_cache/syseeprom_cache
# Then either reboot, or re-run the platform init cache step:
sudo python3 /usr/local/bin/accton_wedge100s_util.py install
```

### 8.4 Kernel mutex for the I2C bus

The Linux I2C core does hold a per-adapter mutex around each complete I2C message
sequence.  This protects against concurrent *kernel* threads racing each other on the
same bus.  The CP2112 problem is at a *lower* level: the USB HID transport breaks what
should be one atomic mux-select+data sequence into two separate USB transactions with no
hardware interlock between them.  Kernel locking cannot fix a gap that exists outside
kernel code.

---

## 9. Platform-Specific Summary

| Device       | Bus  | Address | Type      | Writable? | Content |
|--------------|------|---------|-----------|-----------|---------|
| Host CPLD    | i2c-1| 0x32    | Register  | Some regs | PSU/LED/port control |
| COME EC chip | i2c-1| 0x50    | EC regs   | No        | ODM product ID (factory) |
| System EEPROM| i2c-1| 0x51    | AT24C02   | Yes       | ONIE TlvInfo (per-unit) |
| QSFP ports 1-32| i2c-2..33| 0x50 | optoe  | Yes       | SFF-8472 / CMIS data |
| QSFP presence 0-15 | i2c-36 | 0x22 | PCA9535 | Yes  | GPIO register |
| QSFP presence 16-31| i2c-37 | 0x23 | PCA9535 | Yes  | GPIO register |
| PCA9548 muxes| i2c-1| 0x70-0x74| Mux    | Yes       | Channel select |

**Key insight:** Addresses 0x50–0x53 appear on *every* channel of mux 0x74 (buses 34–41)
because the COME module devices are physically on i2c-1 and the mux is non-isolating for
them.  Registering any at24 device at i2c-1/0x50 via any of those buses actually hits
the EC chip, not a real EEPROM.

---

## 10. Quick Reference: Common One-Liners

```bash
# Scan bus 1 for devices
i2cdetect -y 1

# Read a single CPLD register
i2cget -f -y 1 0x32 0x10

# Read 16 bytes from the system EEPROM (AT24C02, 1-byte addr)
i2ctransfer -f -y 40 w1@0x51 0x00 r16@0x51

# Hex-dump the system EEPROM via sysfs
xxd /sys/bus/i2c/devices/40-0051/eeprom | head -16

# Read the system EEPROM as TlvInfo and print all fields
python3 -c "
from sonic_platform_base.sonic_eeprom.eeprom_tlvinfo import TlvInfoDecoder
e = TlvInfoDecoder('/sys/bus/i2c/devices/40-0051/eeprom', 0, '', False)
e.decode_eeprom(e.read_eeprom())
"

# Read the mux 0x74 channel register directly
python3 -c "
import fcntl
f = open('/dev/i2c-1','rb',buffering=0)
fcntl.ioctl(f, 0x0706, 0x74)  # I2C_SLAVE_FORCE
print('mux 0x74 reg: 0x{:02x}'.format(f.read(1)[0]))
f.close()
"

# Read 8 bytes from any i2c-1 device using 1-byte register addressing
python3 -c "
import fcntl, sys
addr = int(sys.argv[1], 0)
f = open('/dev/i2c-1','r+b',buffering=0)
fcntl.ioctl(f, 0x0706, addr)
f.write(bytes([0x00]))    # register pointer = 0
print(f.read(8).hex())
f.close()
" 0x51

# Register and unregister devices
echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device
echo 0x51 > /sys/bus/i2c/devices/i2c-40/delete_device
```
