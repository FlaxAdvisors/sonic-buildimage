# BEWARE: EEPROM on Accton Wedge 100S-32X

**Read this before touching any EEPROM-related code, driver registration, or I2C init.**
This condenses ~3 months of painful debugging. Every section describes a trap that was
already sprung once.

---

## 1. I2C Address Map — Three Distinct EEPROMs

There are three devices on the CP2112 I2C root bus (i2c-1) at the 0x50–0x51 range, plus the
true system EEPROM behind a mux. They ACK differently and have completely different roles.

| Address | Device | Notes |
|---|---|---|
| `i2c-1 / 0x50` | COME EC chip | ACKs all writes but **silently discards them**. `i2cget 0x50 0x00` returns `0x10`/`0x11` (EC firmware version). `onie-syseeprom -s` targets this address, appears to succeed, but the hardware is unchanged. |
| `i2c-1 / 0x51` | COME module internal EEPROM (AT24C02, ODM format) | TlvInfo data was **mistakenly written here** during early development after the true EEPROM was not found at the expected location. Writing SONiC TlvInfo here partially overwrites COME module factory ODM data. |
| `i2c-40 / 0x50` | **TRUE system EEPROM** (24c64, 8 KiB, ONIE TlvInfo) | Reached via `i2c-1 → mux 0x74 → channel 6`. This is the authoritative platform EEPROM. Sysfs: `/sys/bus/i2c/devices/40-0050/eeprom`. Daemon cache: `/run/wedge100s/syseeprom`. Verified content: PN=20-001688, SN=AI09019591, MAC=00:90:fb:61:da:a1. |

**How to tell them apart:** `i2cget -f -y 1 0x50 0x00` always returns `0x10` or `0x11`
(EC firmware version counter — not EEPROM data). `i2cget -f -y 1 0x51 0x00` returns `0x54`
(ASCII 'T' = TlvInfo magic). The true system EEPROM is only reachable through the mux at
`i2c-40/0x50`.

> **Note on BMC view:** From the BMC (`root@192.168.88.13`, i2c-7), the same physical
> AT24C02 at i2c-1/0x51 is visible at i2c-7/0x51. The BMC's i2c-7 and the host's i2c-1
> share the same physical I2C segment; they compete for SDA/SCL and will time out if both
> are accessed simultaneously.

---

## 2. The Write-Attack Surface — How the Kernel Stack Reaches QSFP 0x50

The canonical kernel path for QSFP EEPROM reads during early SONiC bring-up was:

```
xcvrd → optoe1 sysfs → optoe1 driver → i2c_mux_pca954x → hid_cp2112 → CP2112 USB → PCA9548 mux → QSFP 0x50
```

Every component in this chain is a write hazard:

| Source | Write mechanism |
|---|---|
| `i2c_mux_pca954x` | Probes each PCA9548 channel during driver registration — probe transactions are sent on the live bus while QSFP modules may be selected on 0x50 |
| `optoe1` / `at24` | `at24_probe()` issues a test write to verify EEPROM writability on some AT24 variants; driver registration walks the virtual bus tree and calls `probe()` for each node |
| `i2cdetect` (research/debug) | Default access mode is `SMBUS_QUICK` — a 0-byte write. When issued to address 0x50 with a mux channel active, this is a write transaction to the physical EEPROM |
| `i2cset` (research/debug) | Any `i2cset -y <N> 0x50 ...` on a virtual bus (i2c-2 through i2c-41) reaches the physical QSFP EEPROM if that mux channel is currently selected |
| `dpkg -i` + `systemctl restart pmon` | Each restart reloads `i2c_mux_pca954x` and `optoe1`, re-triggering their full probe sequences across all 32 QSFP buses |

**These budget DAC cable transceivers (FS Q28-PC02/Q28-PC01) have WP permanently unasserted
(tied to GND or unconnected). Any write reaching 0x50 on a selected mux channel overwrites
physical non-volatile EEPROM cells.** The damage is permanent across power cycles.

---

## 3. Corruption Observed (verified on hardware 2026-03-14/15)

Four DAC cable ports and two optical ports were damaged during early platform discovery.

**Pattern A — byte 0 only (Port 8 / Ethernet32):**
- Identifier byte 0 overwritten with `0xb3` (invalid SFF-8024 value)
- Upper page vendor/PN/SN/date fields intact

**Pattern B — byte 0 plus wholesale upper-page corruption (Ports 4 and 12 / Ethernet16, 48):**
- Byte 0 = `0xb3`
- Vendor name bytes 148–163: `Ad\x60\x60\x60,\x02@...` (garbage — not printable)
- Date code bytes 212–219: `000101` (Jan 1, 2000 — factory default, not 2020 manufacture date)
- CC_BASE (byte 191): computed checksum ≠ stored checksum → MISMATCH
- CC_EXT (byte 223): computed checksum ≠ stored checksum → MISMATCH

**Pattern C — syseeprom data blasted into QSFP EEPROM (Port 28 / Ethernet112):**
- `TlvInfo\0` content overlaid starting at byte 2
- SONiC platform syseeprom TlvInfo was written into the transceiver's physical EEPROM
- Vendor SN at bytes 196–211 survived intact (F2032955503-1)

**Optical ports (Ethernet104, Ethernet108):**
- Inserted after early I2C research was complete
- Still exposed to `i2c_mux_pca954x` and `optoe1` probe writes at driver registration
  (which fires on every `dpkg -i` or `systemctl restart pmon`)
- Same corruption pattern: valid identifier byte, garbage vendor, factory-default date code,
  CC_BASE/CC_EXT mismatch
- **The "factory defect" conclusion is not credible.** Multiple independently-sourced modules cannot all arrive with the identical corruption signature.

> **The EOS comparison is instructive:** Arista EOS has been running on the peer Wedge100S
> (192.168.88.14) for 7+ weeks with no EEPROM corruption. EOS loads no kernel mux driver,
> no optoe, no at24. It accesses QSFP EEPROMs through a single daemon that owns
> `/dev/hidraw0` exclusively.

---

## 4. The Fix — hidraw Daemon, No Kernel Mux Drivers

**Current architecture (Phase 2 / production):**

```
wedge100s-i2c-daemon → /dev/hidraw0 → CP2112 (AN495 HID reports) → PCA9548 mux → QSFP 0x50
```

Key properties of the fix:

- `wedge100s-i2c-daemon` opens `/dev/hidraw0` directly and speaks the CP2112 AN495 HID
  protocol. The kernel `hid_cp2112` module is loaded (it manages the USB HID binding) but
  the mux tree is **never handed to the kernel**.
- **`i2c_mux_pca954x` is intentionally NOT loaded.** Bus numbers i2c-2 through i2c-41
  do not exist in the running system.
- **`optoe` and `at24` are intentionally NOT loaded** on QSFP buses.
- `pmon` consumers (xcvrd, etc.) read `/run/wedge100s/sfp_N_eeprom` binary files. They
  never touch I2C directly.
- The daemon is the sole entity that initiates mux-select transactions.
- System EEPROM is read once at first boot via the same hidraw path; result cached at
  `/run/wedge100s/syseeprom`. `sonic_platform/eeprom.py` reads the cache first, falls
  back to sysfs only if the cache is absent.

**To stop the daemon safely before any manual I2C work:**

```bash
sudo systemctl stop wedge100s-i2c-daemon wedge100s-bmc-daemon pmon
```

To resume:
```bash
sudo systemctl start wedge100s-i2c-daemon wedge100s-bmc-daemon pmon
```

**⚠ Do not delete `/run/wedge100s/sfp_N_eeprom` files while the daemon is running.**
Deleting these files triggers immediate EEPROM re-reads on the next daemon tick.
If this happens within 2–5 s of LP_MODE deassert (e.g. a fresh restart), the
module MCU is not yet ready and the upper-page read returns zeros. Byte 220
(DIAG_MON_TYPE, vendor/PN strings) is in the upper page and is NOT refreshed by
the DOM TTL cycle — the zeros persist until the module is re-plugged or the daemon
restarts cleanly. Recovery requires reprogram from the Arista peer (see §3 repair
procedure). This happened in session 2026-03-27; root cause documented in
`notes/2026-03-27-daemon-restart-dom-fix.md`.

**The i2c_topology.json `_NOTICE` block records this as authoritative:**

> `i2c_mux_pca954x, at24, and optoe are intentionally NOT loaded.`
> `Bus numbers i2c-2 through i2c-41 do NOT exist in the running system.`
> `All QSFP EEPROM and system EEPROM access is via /dev/hidraw0 (wedge100s-i2c-daemon).`

---

## 5. The `onie-syseeprom` Trap

> **`onie-syseeprom -s` reports "Programming passed" even when it wrote nothing.**

How this trap works:
1. `onie-syseeprom -s ...` encodes the TlvInfo record in memory.
2. It attempts to write the bytes to `i2c-1/0x50` — the EC chip.
3. The EC chip ACKs the write but discards all bytes (undefined register space echoes
   transiently; low registers 0x00–0x0F are ROM-backed and cannot be overwritten).
4. The "verification" step reads from the **in-memory buffer**, not from hardware.
5. It outputs CRC `0x69F352D5` (which happens to match the TlvInfo previously written
   to `i2c-1/0x51` during early dev) and declares success.
6. A subsequent `onie-syseeprom` (no `-s`) reads from hardware at `0x50` → EC chip →
   "Invalid TLV header".

**Do not trust `onie-syseeprom` output on this platform.** To verify what is actually in
hardware, read the raw bytes directly:

```bash
# From ONIE or SONiC with daemon stopped:
i2cget -f -y 1 0x50 0x00    # should return 0x10 or 0x11 (EC version) — NOT 0x54 (TlvInfo)
i2cget -f -y 1 0x51 0x00    # returns 0x54 if our TlvInfo is there
# True system EEPROM (mux must be selected):
i2cset -y 1 0x74 0x40        # select mux 0x74 channel 6
i2cget -y 1 0x50 0x00        # should return 0x54 (TlvInfo magic 'T')
i2cset -y 1 0x74 0x00        # deselect mux
```

---

## 6. Do Not Revert — Loading `i2c_mux_pca954x` Will Immediately Re-Corrupt

> **Loading `i2c_mux_pca954x` is an immediate EEPROM corruption event if any QSFP
> modules are inserted.**

Driver registration probes every PCA9548 channel. With 5 muxes × 8 channels × the
AT24/optoe probe write pattern, every inserted transceiver at 0x50 is at risk during
the ~2 seconds of probe activity.

This is not theoretical — it is the mechanism by which 6 transceivers were corrupted
during development.

**Never do any of the following unless you have physically removed all QSFP modules first:**

- `modprobe i2c_mux_pca954x`
- `modprobe optoe`
- `modprobe at24` (when any QSFP virtual bus might be selected)
- Any `dpkg -i` of the platform module package while pmon is running (stops pmon first,
  which would leave `i2c_mux_pca954x` loaded from the previous install)
- Running `i2cdetect -y <N>` on any bus number 2–41 while a QSFP is inserted and the
  mux channel for that bus is selected

**The correct upgrade path is:**

```bash
sudo systemctl stop pmon
sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
sudo systemctl start pmon
```

The platform `.deb` is designed to not load `i2c_mux_pca954x` or `optoe`. Verify after
any package change that `lsmod | grep -E 'pca954|optoe|at24'` returns empty output.

---

## Quick Reference

| Want to… | Safe way | Unsafe way (do not do) |
|---|---|---|
| Read system EEPROM | `cat /run/wedge100s/syseeprom` or `show platform syseeprom` | `onie-syseeprom` (reads EC chip, not EEPROM) |
| Read QSFP EEPROM | `cat /run/wedge100s/sfp_N_eeprom` | `i2cget -y <N> 0x50 ...` while daemon is running |
| Debug I2C bus | Stop daemon first: `systemctl stop wedge100s-i2c-daemon wedge100s-bmc-daemon pmon` | Any i2c tool while daemon is running |
| Verify system EEPROM content | `xxd /run/wedge100s/syseeprom \| head` — look for `TlvInfo` at offset 0 | `i2cget -y 1 0x50 0x00` — returns EC version byte, not EEPROM |
| Load kernel mux driver | **Don't.** Remove all QSFPs first if you must. | `modprobe i2c_mux_pca954x` with modules inserted |
