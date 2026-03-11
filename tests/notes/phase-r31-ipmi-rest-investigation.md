# Phase R31 — IPMI / OpenBMC REST Investigation

*Completed 2026-03-11, verified on hardware (SONiC kernel 6.12.41, hare-lorax)*

---

## Summary

Three channels investigated: IPMI KCS (in-band), IPMI over LAN, and USB CDC-ECM REST API.
IPMI is completely absent. The REST API works over IPv6 link-local on `usb0` but does not
improve on our current architecture (C daemon → `/run/wedge100s/` files).

---

## 1. IPMI KCS (in-band, `/dev/ipmi0`)

```
admin@hare-lorax:~$ sudo modprobe ipmi_si
modprobe: ERROR: could not insert 'ipmi_si': No such device
```

**Result: Not available.** No KCS/BT/SMIC interface is exposed to the host CPU.
Confirmed on BMC side: `ls /sys/class/ipmi/ → no ipmi class`, `ls /dev/ipmi* → nothing`.

---

## 2. IPMI over LAN (UDP/623)

```
$ ipmitool -H 192.168.88.13 -U root -P 0penBmc sdr list
Error: Unable to establish LAN session

$ ipmitool -H 192.168.88.13 -U root -P 0penBmc -I lanplus sdr list
Error: Unable to establish IPMI v2 / RMCP+ session
```

BMC UDP ports: only 123 (NTP) and 68 (DHCP client). Port 623 is not listening.

**Result: Not available.** This is a Facebook-OpenBMC build predating IPMI support.

---

## 3. USB CDC-ECM Network (`usb0`) + Facebook REST API

### Interface state

| Side   | State at boot | Link-local (after `ip link set usb0 up`) |
|--------|---------------|-------------------------------------------|
| SONiC  | DOWN          | `fe80::ff:fe00:2/64`                      |
| BMC    | UP            | `fe80::ff:fe00:1/64`                      |

The SONiC `usb0` comes up but has no address configured at boot. After manual
`sudo ip link set usb0 up`, IPv6 link-local autoconfigures and the BMC is reachable:

```
ping6 -c2 -I usb0 fe80::ff:fe00:1   →   0% loss, ~0.8ms RTT
```

### Facebook REST API (port 8080)

The BMC runs a Facebook-OpenBMC Python REST server (aiohttp) on ports 8080 and 7027.
No TLS is configured (`/mnt/data/etc/host_server.pem` missing); only port 8443 would be
SSL — it is not listening.

**Available endpoints** (verified via `wget` on BMC localhost):

| Endpoint                      | Content                                      |
|-------------------------------|----------------------------------------------|
| `GET /api`                    | Version + Resources                          |
| `GET /api/sys`                | Resource list                                |
| `GET /api/sys/bmc`            | BMC version, MAC, uptime, memory, load       |
| `GET /api/sys/sensors`        | All thermal sensors + fan RPMs (see below)   |
| `GET /api/sys/gpios`          | {} (empty, not implemented for wedge100s)    |
| `GET /api/sys/fc_present`     | Not applicable (no FC cards)                 |
| `GET /api/sys/firmware_info`  | Board firmware versions                      |
| `POST /api/sys/server`        | Power on/off/reset (not fan speed)           |
| `POST /api/sys/psu_update`    | PSU firmware update only                     |

**No fan speed write endpoint exists.** `writable = false` in `/etc/rest.cfg`.
The only POST actions are host power control and PSU firmware update.

### Sensor data format from `/api/sys/sensors`

One HTTP call returns all BMC-visible sensors (verified 2026-03-11):

```json
[
  {"name": "tmp75-i2c-3-48",  "Outlet Middle Temp": "+26.4 C"},
  {"name": "tmp75-i2c-3-49",  "Inlet Middle Temp":  "+24.4 C"},
  {"name": "tmp75-i2c-3-4a",  "Inlet Left Temp":    "+24.5 C"},
  {"name": "tmp75-i2c-3-4b",  "Switch Temp":        "+38.0 C"},
  {"name": "tmp75-i2c-3-4c",  "Inlet Right Temp":   "+23.0 C"},
  {"name": "com_e_driver-i2c-4-33",
   "CPU Temp": "+50.0 C", "Memory Temp": "+34.5 C",
   "+12V Voltage": "+12.25 V", "CPU Vcore": "+1.79 V", ...},
  {"name": "fancpld-i2c-8-33",
   "Fan 1 front": "7500 RPM", "Fan 1 rear": "4950 RPM",
   "Fan 2 front": "7500 RPM", "Fan 2 rear": "4950 RPM", ...},
  {"name": "tmp75-i2c-8-48",  "Outlet Right Temp":  "+22.6 C"},
  {"name": "tmp75-i2c-8-49",  "Outlet Left Temp":   "+24.4 C"}
]
```

Thermal sensor names in JSON match our existing `/run/wedge100s/thermal_N` mapping:

| REST field           | bmc_daemon label   | Our thermal index |
|----------------------|--------------------|-------------------|
| Inlet Middle Temp    | tmp75-i2c-3-49     | thermal_1         |
| Inlet Left Temp      | tmp75-i2c-3-4a     | thermal_2         |
| Inlet Right Temp     | tmp75-i2c-3-4c     | thermal_3         |
| Outlet Middle Temp   | tmp75-i2c-3-48     | thermal_4         |
| Outlet Right Temp    | tmp75-i2c-8-48     | thermal_5         |
| Outlet Left Temp     | tmp75-i2c-8-49     | thermal_6         |
| Switch Temp          | tmp75-i2c-3-4b     | thermal_7         |

Fan labels "Fan N front/rear" map directly to our `fan_N_front` / `fan_N_rear` convention.

### Latency

```
run 1: 1.025s   (REST API, fetches all sensors)
run 2: 1.379s
run 3: 1.290s
```

The REST server calls `sensors` on each request — that's the floor.
Compare to our current architecture: C daemon updates `/run/wedge100s/` every ~3s;
Python reads those files in <1ms.

---

## 4. Architecture Assessment

| Channel           | Verdict      | Reason                                               |
|-------------------|--------------|------------------------------------------------------|
| IPMI KCS          | ✗ Dead end   | No KCS interface exposed to host                     |
| IPMI over LAN     | ✗ Dead end   | Port 623 not listening; BMC doesn't run ipmid        |
| REST over usb0    | Viable, but slower | ~1.3s/call vs <1ms from /run/wedge100s/ files  |
| Current (C daemon)| Best         | 3s background cycle, sub-ms reads in Python          |

**Conclusion**: The REST API is functional and returns clean JSON with all the sensor/fan
data we need. However, it does not beat our existing C daemon architecture:

- Our daemon runs one TTY session and caches all data to `/run/wedge100s/` every 3s
- Python reads `/run/wedge100s/thermal_N` etc. in under 1ms
- REST adds 1-1.4s per pmon poll cycle (REST server subprocess overhead) and requires
  `usb0` to be configured at boot

**Remaining TTY usage** (set_fan_speed.sh): the REST API has no fan speed write endpoint
(`writable = false`; only power control and PSU update POSTs exist). The TTY write path
for fan speed cannot be replaced by REST.

---

## 5. Potential Future Uses of REST API

Not actionable now, but worth noting:

1. **Redundant fallback**: if C daemon dies, REST can provide sensor readings without TTY
2. **Host power control** (`POST /api/sys/server {"action":"power-reset"}`) is a useful
   BMC capability already available over usb0 if `usb0` gets a persistent address
3. **`/api/sys/bmc`** gives BMC uptime/load — useful for diagnostics
4. **`/api/sys/firmware_info`** — firmware version auditing

If `usb0` were given a persistent IPv6 link-local address via systemd-networkd, the REST
API would be available without manual bring-up. One-liner to configure:
```
# /etc/systemd/network/50-usb0.network
[Match]
Name=usb0
[Network]
LinkLocalAddressing=ipv6
```
This is not needed for current functionality.

---

## 6. Recommendation

**Phase R31 is complete. No code changes required.**

The R28 C daemon + R29 file-read approach already represents the best achievable latency
for BMC sensor polling. The REST API is a confirmed working fallback but not an upgrade.
The only remaining TTY dependency (`set_fan_speed.sh` in `bmc.py`) cannot be replaced
via REST due to missing write endpoint.
