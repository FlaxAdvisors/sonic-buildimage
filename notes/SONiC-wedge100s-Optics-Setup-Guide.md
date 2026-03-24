# SONiC Wedge100S-32X — Optical Port Operator Reference

(verified on hardware 2026-03-20)

---

## 1. Hardware Overview

Four QSFP28 100G optical ports are available:

| SONiC Port | Port Index | Module | Type | Fiber | Arista Peer |
|------------|-----------|--------|------|-------|-------------|
| Ethernet100 | 25 | Arista QSFP28-SR4-100G (SN G2120113967) | 100GBASE-SR4 | MPO-12 OM3/4 | rabbit-lorax Et26/1 |
| Ethernet104 | 26 | Arista QSFP28-LR4-100G (SN S2109025969) | 100GBASE-LR4 | LC duplex SMF | rabbit-lorax Et27/1 |
| Ethernet108 | 27 | Arista QSFP28-SR4-100G (SN G2120114779) | 100GBASE-SR4 | MPO-12 OM3/4 | rabbit-lorax Et28/1 |
| Ethernet116 | 29 | ColorChip CWDM4 (SN 17314400) | 100G CWDM4 | LC duplex SMF | rabbit-lorax Et30/1 |

**LP_MODE**: Managed by `wedge100s-i2c-daemon` via PCA9535 GPIO (mux 0x74 ch0/1, addrs 0x20/0x21).
The daemon deasserts LP_MODE for all present modules on first boot. State files: `/run/wedge100s/sfp_N_lpmode`.

**RESET**: Not accessible from host CPU. BCM ASIC has no accessible GPIO. BMC SYSCPLD has no per-port QSFP RESET sysfs.

---

## 2. Transceiver CLI Command Reference

| Command | STATE_DB source | Works for | Notes |
|---------|----------------|-----------|-------|
| `show interfaces transceiver presence` | `TRANSCEIVER_INFO\|EthernetX` | all | |
| `show interfaces transceiver eeprom EthernetX` | `TRANSCEIVER_INFO\|EthernetX` | all | |
| `show interfaces transceiver eeprom --dom EthernetX` | `TRANSCEIVER_DOM_SENSOR\|EthernetX` | all with DOM data | modules with `dom_capability: N/A` still render via demand-driven lower-page read |
| `show interfaces transceiver info EthernetX` | `TRANSCEIVER_INFO\|EthernetX` | all | "SFP EEPROM detected" when xcvrd has populated the table |
| `show interfaces transceiver status EthernetX` | `TRANSCEIVER_STATUS\|EthernetX` + `TRANSCEIVER_STATUS_SW` | ports with modules | absent ports show "not applicable" (expected) |
| `show interfaces transceiver pm EthernetX` | `TRANSCEIVER_DOM_SENSOR\|EthernetX` | SFF-8636 (4-lane table) | ZR/CMIS ports use `TRANSCEIVER_PM` table instead |
| `show interfaces transceiver lpmode EthernetX` | sfputil hardware read | all | reads `/run/wedge100s/sfp_N_lpmode` |
| `show interfaces transceiver error-status` | sfputil | all | |

---

## 3. DOM Architecture — Demand-Driven Refresh

DOM data (Rx/Tx power, bias, temperature) flows through two layers:

1. **`wedge100s-i2c-daemon`** (C, runs every 3 s via systemd timer):
   - Writes EEPROM cache `/run/wedge100s/sfp_N_eeprom` on **insertion only** (not every tick).
   - Stable ports (valid SFF identifier byte in cache) skip the EEPROM I2C read entirely.
   - This prevents CP2112 USB-HID bus saturation that previously caused SSH unresponsiveness.

2. **`sfp.py` demand-driven refresh** (`_DOM_CACHE_TTL = 10 s`):
   - When xcvrd calls `read_eeprom(offset < 128)` and TTL has expired, performs a live smbus2 lower-page (128 bytes) read via CP2112 bus 1 → PCA9548 mux → I2C 0x50.
   - Merges new lower page with cached upper page, atomically replaces `/run/wedge100s/sfp_N_eeprom`.
   - All four modules show live DOM readings despite `dom_capability: N/A` in their EEPROM.

---

## 4. DOM Sensor Reference Values (2026-03-20)

| Port | Module | Rx Power | Tx Bias | Tx Power | Temp | Voltage |
|------|--------|----------|---------|----------|------|---------|
| Ethernet100 | SR4 | **-inf dBm** (Rx LOS) | 6.5 mA | -0.2 to -0.8 dBm | N/A | N/A |
| Ethernet104 | LR4 | -0.63 to -1.0 dBm | 43–47 mA | 0.85–2.66 dBm | 34.6 °C | 3.28 V |
| Ethernet108 | SR4 | 0.11–0.36 dBm | 6.5 mA | -0.2 to -1.0 dBm | N/A | N/A |
| Ethernet116 | CWDM4 | -0.11 to +0.79 dBm | 39–54 mA | 0.98–1.82 dBm | 35.4 °C | 3.23 V |

Note: Arista SR4 modules (Ethernet100, 108) report `temperature: N/A` and `voltage: N/A` — these
registers are not implemented in this variant. TX bias of 6.5 mA is the reported value at
the current SerDes drive level; Ethernet108 linked successfully at this bias.

Rx power thresholds (spec): SR4 0 to −9.5 dBm, LR4 0 to −14.4 dBm, CWDM4 0 to −9.5 dBm.

---

## 5. Link Status (2026-03-20)

| Port | Oper | Admin | FEC | Finding |
|------|------|-------|-----|---------|
| Ethernet100 | **down** | up | rs | Physical Rx LOS — fiber from Arista Et26/1 not reaching Ethernet100 Rx. Arista TX = -0.23 dBm (laser on), SONiC Rx = -inf. Also: BCM NPU_SI_SETTINGS_DEFAULT (TXAMP=8), byte 3=0x0f (host TX LOS from ASIC). |
| Ethernet104 | **down** | up | none | BCM SerDes SD=0 on all 4 Rx lanes (no signal detect). Module byte 34=0x1e (all 4 Tx host-electrical lanes LOS — ASIC not driving module), byte 9=0x00 (Rx CDR locked optically), ASIC TXAMP=8 too low for LR4 module LOS threshold. NPU_SI_SETTINGS_DEFAULT. Arista Et27/1 receives 1.24 dBm from us — TX optical path good. |
| Ethernet108 | **UP** | up | rs | Linked. Ethernet108 ↔ rabbit-lorax Et28/1 (confirmed via LLDP). |
| Ethernet116 | **down** | up | rs | Arista Et30/1 TX = -30 dBm (laser not transmitting). Et30/1 Rx = +0.31 dBm (receiving our signal). SONiC Rx = +0.79 dBm (source uncertain — Et30/1 laser or loopback). |

---

## 6. FEC Configuration

| Port | SONiC FEC | Arista FEC | Notes |
|------|-----------|------------|-------|
| Ethernet100 | rs | default | SR4: RS-FEC is standard (IEEE 802.3bm) |
| Ethernet104 | none | none (default) | LR4: no FEC per IEEE 802.3ba. Arista EOS 4.27.0F has no explicit `fec` subcommand on this platform |
| Ethernet108 | rs | default | SR4: RS-FEC, confirmed working |
| Ethernet116 | rs | default | CWDM4: RS-FEC per CWDM4 MSA |

---

## 7. Link Bring-Up Procedure

For a newly-connected optical port:

1. **Verify presence**: `show interfaces transceiver presence EthernetX`
2. **Verify EEPROM readable**: `show interfaces transceiver eeprom EthernetX`
3. **Check TX disable state**: `show interfaces transceiver status EthernetX`
   - If any `TX disable status on lane N: True` → restart pmon:
     ```bash
     sudo systemctl restart pmon && sleep 20
     ```
   - If still disabled after restart → direct register write (last resort):
     ```bash
     sudo sfputil write-eeprom -p EthernetX -n 0 -o 86 -d 00
     ```
4. **Check DOM**: `show interfaces transceiver pm EthernetX`
   - TX bias should be > 20 mA for SR4/LR4/CWDM4 in normal operation
   - Rx power > −14 dBm (LR4) or > −9.5 dBm (SR4/CWDM4) if fiber is connected
5. **Align FEC with peer**: `sudo config interface fec EthernetX none|rs`
6. **Wait for link convergence** (allow 30 s for RS-FEC training)
7. **Verify**: `show interfaces status EthernetX`

---

## 8. Troubleshooting

### Rx LOS on all 4 lanes despite fiber connected (SR4)
- Check MPO-12 polarity: SR4↔SR4 requires **Type B** (key-up to key-down). Type A reverses lane order → all-lane Rx LOS.
- Check Arista peer TX: `show interfaces EthernetN/1 transceiver` — verify TX power > −9 dBm.
- Check LP_MODE: `cat /run/wedge100s/sfp_25_lpmode` — should be `0`. If `1`, daemon hasn't deasserted yet; restart pmon.

### TX bias stuck at 6.5 mA (Arista SR4 modules)
- Observed on Ethernet100 and Ethernet108. Ethernet108 came up despite this.
- 6.5 mA may reflect NPU_SI_SETTINGS_DEFAULT (low SerDes TXAMP). Platform BCM SI settings tuning needed for full amplitude.
- Do NOT confuse with LP mode (LP mode = 0 mA; 6.5 mA = low-power operation still lasing).

### Link down despite bidirectional signal (LR4 / CWDM4)
- Check BCM SerDes: SD=0 indicates ASIC not detecting electrical Rx from module.
- Root cause: NPU_SI_SETTINGS_DEFAULT — BCM TXAMP=8 may be below module's host-input LOS threshold.
- Diagnostic: Read SFF-8636 byte 34 (LOS register). `0x1e` = bits 4:1 set = all 4 Tx host-electrical lanes show LOS (ASIC → module path broken). Bits 3:0 = Rx optical side — if 0x0, the incoming fiber is fine and the fault is purely on the host-electrical side.
  ```bash
  sudo sfputil read-eeprom -p EthernetX -n 0 -o 34 -s 1
  ```
- **Permanent fix**: Edit `/usr/share/sonic/hwsku/th-wedge100s-32x-flex.config.bcm` to add per-port TXAMP/TXEQ SerDes SI settings so `NPU_SI_SETTINGS_SYNC_STATUS` moves from `DEFAULT` to a calibrated value. The SAI profile at `/etc/sonic/sai.profile` references this file via `SAI_INIT_CONFIG_FILE`.
- Workaround: Check if a `td3-ds-*` BCM config or `sai_profile` in `/etc/sonic/` specifies per-port TXAMP.

### Module reports "not applicable" in `show interfaces transceiver status`
- Expected behavior for absent/removed ports (cmis_state=REMOVED).
- If an installed optical module shows "not applicable", check `TRANSCEIVER_STATUS|EthernetX` in STATE_DB: if empty, xcvrd hasn't written it yet. Restart pmon.

### PM command shows no data
- Verify `TRANSCEIVER_DOM_SENSOR|EthernetX` is populated: `sudo redis-cli -n 6 HGETALL 'TRANSCEIVER_DOM_SENSOR|EthernetX'`
- DOM data is refreshed on-demand every 10 s when xcvrd reads the port. If xcvrd hasn't polled it yet (e.g. port just came up), wait one xcvrd cycle (~3 s) and retry.

---

## 9. Known Platform Limitations

- **BCM SI Settings**: All QSFP28 ports report `NPU_SI_SETTINGS_DEFAULT`. A platform-specific BCM serdes config is required to tune TXAMP/TXEQ for each port's trace length and module type. Without it, some module types (LR4) may not detect the ASIC's electrical output.
- **RESET**: Not accessible from host CPU. Module RESET can only be performed via BMC GPIO if a future SYSCPLD sysfs attribute is added.
- **Temperature/Voltage**: Not reported by Arista QSFP28-SR4-100G modules on this platform — returns N/A. This is a module capability limitation, not a platform bug.
