# L2 Completion Design — Wedge 100S-32X SONiC Port

**Date:** 2026-03-22
**Branch:** wedge100s
**Status:** Sections 1–3 approved. Section 4 pending approval.

---

## Background

Phases 00–22 of the Wedge 100S-32X SONiC port are complete (99.6% pass rate, 230/231
tests). Three functional gaps remain before the platform can be considered production-ready:

1. No default `config_db.json` — fresh installs boot into a BGP-flooding T0 topology
2. No throughput verification — L2 forwarding correctness is tested but line-rate is not
3. Incomplete `--report` coverage — 11 of ~21 stage directories have no human-readable
   reporter (stages 09–12, 14–16, 19–21, plus stage_23 once implemented)

This spec closes all three gaps and adds 100G switch-to-switch performance verification.

---

## Section 1 — Default `config_db.json`

### Goal

Ship a minimal safe baseline configuration with the SONiC image so that a freshly
installed switch boots into a known-good state without operator intervention.

### Problem

Without a device-specific `config_db.json`, SONiC generates a default T0 topology on
first boot: 32 BGP neighbors, L3 IPs on all data ports. BGP sends ARP/ND out every
port (including oper-down ports), flooding the management plane and making SSH
semi-responsive. The current workaround (`sudo config feature state bgp disabled`)
must be run manually after every fresh install.

### Solution

Place `config_db.json` at:

```
device/accton/x86_64-accton_wedge100s_32x-r0/config_db.json
```

The SONiC installer copies this file to `/etc/sonic/config_db.json` during image
installation. **Implementation note:** verify which installer hook performs this copy
(`installer/install.sh`, the lazy-install `.deb` postinst, or a first-boot service)
and confirm the existence-check logic ("only write if `/etc/sonic/config_db.json`
does not already exist") is in place before coding.

### Contents

| Table | Entries | Purpose |
|---|---|---|
| `DEVICE_METADATA\|localhost` | `default_bgp_status: down`, correct `platform` and `hwsku` strings | Belt-and-suspenders with FEATURE; also sets BGP router-id context |
| `FEATURE\|bgp` | `state: disabled` | **Stops the BGP container entirely** — no ARP/ND flooding possible. This is the primary mechanism; it matches the manual workaround from TODO.md (`config feature state bgp disabled`). `default_bgp_status: down` provides belt-and-suspenders if bgp is later re-enabled without a full config change. |
| `PORT\|EthernetN` ×32 | `speed: 100000`, `fec: rs`, `admin_status: up` | All ports at 1×100G with RS-FEC; safe default for Tomahawk with mixed DAC/optical |
| `MGMT_VRF_CONFIG\|vrf_global` | `mgmtVrfEnabled: true` | Triggers `hostcfgd` → `interfaces-config` → `ifup` chain on first boot to create the kernel VRF, master eth0, and install the mgmt routing table |

### What is NOT included

- No VLANs, no VLAN members — deploy.py `VlanTask` owns these
- No PortChannel — deploy.py `PortChannelTask` owns this
- No port breakouts — deploy.py `BreakoutTask` owns this
- No L3 interfaces, no IP addresses, no static routes
- No BGP neighbors
- No autoneg — unsafe to enable globally (optical modules do not support 100G AN;
  DAC ports can use it but RS-FEC is sufficient for reliable bring-up)
- No STP — not compiled into this SONiC build; VLAN 10 loop risk is accepted
  (all VLAN 10 members connect to end-hosts, not other switches)

### Deploy.py impact

`MgmtVrfTask.check()` queries redis for `MGMT_VRF_CONFIG|vrf_global mgmtVrfEnabled`.
On a freshly imaged, fully-converged switch, this will return `true` and the derived
checks (eth0 mastered, default route in table 5000) will also pass — making the task
a no-op. During the boot window before `hostcfgd` and `interfaces-config` have
converged, the derived checks may still fire and `apply()` may run. This is safe and
correct — deploy.py should always be run against a fully-booted switch. The mgmt_vrf
task remains in TASK_ORDER for in-place upgrades that do not re-image.

---

## Section 2 — Phase 23 Throughput Test

### Goal

Verify that the Wedge 100S-32X forwards traffic at or near line rate for 10G, 25G,
and 100G links under realistic host-to-host and switch-to-switch conditions.

### Location

```
tests/stage_23_throughput/
    __init__.py
    test_throughput.py
    conftest.py          (iperf3 availability fixture, host SSH fixtures)
```

### Test Matrix

| Test | Ports | Traffic Path | Pass Threshold | Notes |
|---|---|---|---|---|
| `test_throughput_10g` | Ethernet66 ↔ Ethernet67 | host → switch → host (VLAN 10) | ≥ 8 Gbps | 10G subports of Ethernet64 4×10G breakout |
| `test_throughput_25g_pair1` | Ethernet80 ↔ Ethernet81 | host → switch → host (VLAN 10) | ≥ 20 Gbps | Both hosts confirmed reachable |
| `test_throughput_25g_pair2` | Ethernet0 ↔ Ethernet1 | host → switch → host (VLAN 10) | ≥ 20 Gbps | **Expected skip** — Ethernet1 is a confirmed dark lane (TODO.md); test skips if host at `topology.json hosts[].mgmt_ip` for Ethernet1 is unreachable |
| `test_throughput_cross_qsfp` | Ethernet66 ↔ Ethernet80 | cross-QSFP via VLAN 10 | ≥ 8 Gbps | 10G bottleneck port; cross-QSFP path exercises switching fabric |
| `test_throughput_100g_eth48` | Ethernet48 ↔ EOS Et15/1 | direct iperf3 to EOS bash | ≥ 90 Gbps | |
| `test_throughput_100g_eth112` | Ethernet112 ↔ EOS Et16/1 | direct iperf3 to EOS bash | ≥ 90 Gbps | |

### Host-to-Host Mechanics (10G / 25G tests)

- Hosts defined in `tools/topology.json` `hosts[]` with `mgmt_ip` (SSH) and
  `test_ip` (iperf3 data plane, VLAN 10)
- `target.cfg` `[hosts]` section provides `ssh_user` and `key_file`
- Fixture SSHes to both endpoints, starts `iperf3 -s` on one, runs
  `iperf3 -c <test_ip> -t 10 --json` on the other, parses `sum_received.bits_per_second`
- 10-second test duration

### 100G Switch-to-Switch Mechanics

- EOS is directly reachable at `admin@192.168.88.14` — no jump host needed. PortChannel1
  carries no IP address (L2-only, VLAN 999), so the dev-host management path to EOS
  is a direct L2 path. See `tests/notes/lacp-mgmt-reachability-root-cause.md`.
- EOS peer ports: Ethernet48 → Et15/1, Ethernet112 → Et16/1 (confirmed via LLDP,
  STAGED_PHASES.md Phase 13)
- Setup: assign temp `/30` IPs on both sides via SONiC CLI and EOS `bash ip addr add`
- Run `iperf3 -s` on EOS bash, `iperf3 -c <eos_ip> -t 10 --json` from SONiC
- Assert `bits_per_second ≥ 90e9` (90% of 100G line rate)
- Teardown: remove temp IPs from both sides regardless of test outcome — use pytest
  `yield` fixture to guarantee cleanup even on assertion failure

### Prerequisites and Skip Logic

All skip conditions produce `pytest.skip()` with a descriptive message — they are
not test failures:

| Condition | Skip message |
|---|---|
| `iperf3` absent on a host | `"iperf3 not found on host <mgmt_ip> — install iperf3 and retry"` |
| Host SSH unreachable | `"Host <mgmt_ip> (port <topology_port>) not reachable via SSH"` |
| `iperf3` absent on EOS | `"iperf3 not found in EOS bash — cannot run 100G switch-to-switch test"` |
| Ethernet1 host unreachable | `"Ethernet1 dark lane (see TODO.md) — test_throughput_25g_pair2 skipped"` |

### STAGED_PHASES.md update

Mark Phase 23 status as COMPLETE on implementation. Update overall pass rate to
reflect the new test count.

---

## Section 3 — `--report` Expansion

### Goal

Extend `tests/lib/report.py` so that `run_tests.py --report` produces a human-readable
hardware state snapshot for all implemented stages.

### Scope

Currently 9 reporters exist covering stages 01–08 and 13. This section adds 11 entries
to the `REPORTERS` dict: 10 active reporters (stages 09–12, 14–16, 19–21) and 1
placeholder (stage 23).

Stages intentionally **excluded** from reporter expansion:
- `stage_00_pretest` — read-only audit; its output is only meaningful as a pass/fail gate, not a report
- `stage_17_report` — already generates a file-based platform status report; no additional reporter needed
- `stage_nn_posttest` — health-check-only; no tabular hardware data to report

### New Reporters

| Stage | Reporter content |
|---|---|
| `stage_09_cpld` | CPLD version register, PSU present/pgood bits from sysfs, LED register raw values |
| `stage_10_daemon` | `i2c-poller.timer` active state and last-trigger time; cache file count and age in `/run/wedge100s/`. **Excludes** bmc-poller timer and `/run/wedge100s/` file count — already reported by `report_platform` (stage 03) |
| `stage_11_transceiver` | `TRANSCEIVER_INFO` and `TRANSCEIVER_DOM_SENSOR` from STATE_DB for all present ports |
| `stage_12_counters` | `show interfaces counters` for link-up ports — RX/TX packets, errors, utilisation |
| `stage_14_breakout` | Active breakout mode per parent port from `CONFIG_DB PORT` table vs `platform.json` defaults |
| `stage_15_autoneg_fec` | FEC mode and autoneg state for all connected ports from `APP_DB PORT_TABLE` |
| `stage_16_portchannel` | PortChannel1 members, LACP state via `teamdctl state`, VLAN membership |
| `stage_19_platform_cli` | Base MAC, reboot cause, CPLD/BIOS version, watchdog status — one row per CLI item |
| `stage_20_traffic` | TX/RX counter deltas for Ethernet16/32 from COUNTERS_DB (last observed values) |
| `stage_21_lpmode` | LP_MODE state per installed SFP from `/run/wedge100s/sfp_N_lpmode` cache files |
| `stage_23_throughput` | Placeholder: `"Run pytest stage_23_throughput -v for live throughput results"` |

### Registry update

Add all 11 new entries to the `REPORTERS` dict at the bottom of `report.py` and
ensure `_available_stages()` in `run_tests.py` picks up `stage_23_throughput` once
the directory exists.

### Cleanup

Remove `tools/tasks/mgmt_vrf.py~` (stale editor backup file, visible in `git status`).

---

---

## Section 4 — I2C Daemon Architecture Hardening

### Goal

Reduce steady-state I2C bus traffic and eliminate the two remaining sources of
management-plane latency spikes: timer-driven presence polling and cross-process
bus contention between the daemon and pmon.

### Background

Three generations of optimization already exist (per `docs/superpowers/plans/`):
- BMC: 28 serial TTY commands (65s/cycle) → single daemon session every 10s
- EEPROM: per-tick bus reads → insertion-triggered cache + 10s DOM TTL
- LP_MODE: direct smbus2 → daemon-owned request/response files (Phase 21)

This section extends that pattern to its logical conclusion:
full daemon bus ownership and bank-interleaved DOM reads. INT_L interrupt-driven
presence is a feasibility-gated investigation item.

BMC REST API (Redfish) was investigated and rejected — this Facebook OpenBMC build
does not expose a REST interface. USB-CDC-Ethernet is the alternative for BMC
in-band communication and is tracked as an investigation item below.

---

### Item 4a — INT_L Interrupt-Driven Presence Detection (Feasibility-Gated)

**Current state:** `wedge100s-i2c-daemon` is a one-shot binary invoked by a systemd
timer every 3 seconds. It opens `/dev/hidraw0`, polls PCA9535 presence registers,
writes cache files, and exits. In steady state this generates ~2.9M PCA9535 reads
per month with zero state changes.

**The INT_L signal path:** Per `notes/i2c_topology.json`, QSFP INT_L signals are
aggregated into two PCA9535 I/O expanders (0x24 and 0x25) reached via mux 0x74
ch4/ch5. The PCA9535 has its own INT output pin that fires on any input change.
**However:** whether that PCA9535 INT pin is wired to a host CPU GPIO (rather than
to the BMC) is unknown — no schematic or ONL source confirms this wiring on the
Wedge 100S-32X.

**Design (conditional on feasibility):**

*Phase 1 — Investigation (hardware verification required first):*

1. Check `/sys/class/gpio/` for available GPIO lines on the host
2. Cross-reference with ONL `platform_lib.c` or schematic for any GPIO mapped to
   PCA9535 INT output
3. If no host CPU GPIO is wired: document in `tests/notes/` and accept extended
   polling (10s timer) as the permanent presence-detection mechanism — 4a is closed

*Phase 2 — Implementation (only if host GPIO confirmed):*

This also requires converting the daemon from **one-shot** (exits after each
invocation) to **persistent** (long-running process) — a significant architectural
change that must be explicitly scoped:

1. Replace systemd one-shot timer with a `Type=simple` persistent service
2. Daemon main loop: `poll(gpio_fd, POLLPRI, -1)` blocks until PCA9535 INT edge
3. On wakeup: read both PCA9535 chips, update `/run/wedge100s/sfp_N_present`
4. Re-arm: `lseek(gpio_fd, 0, SEEK_SET)` to clear the edge event
5. Fallback: 10s watchdog timer re-reads presence if no INT fires (safety net)

**Outcome (if feasible):** Near-zero I2C bus traffic for presence in steady state.
**Outcome (if GPIO unavailable):** Presence polling moved from 3s to 10s timer;
one-shot daemon architecture preserved.

---

### Item 4b — Full Daemon I2C Ownership

**Current state:** pmon (`sfp.py`) writes to `/dev/i2c-1` directly via smbus2 for
`write_eeprom()` calls (TX_DISABLE, power control byte 93). This creates two
independent kernel interfaces to the same physical CP2112 bus with no cross-process
mutual exclusion. A write collision during a daemon poll tick can corrupt mux state;
the kernel resolves this with a bus reset that briefly removes the CP2112 USB device,
causing measurable scheduler latency spikes.

**Design:**

Extend the LP_MODE request-file pattern (Phase 21) to all pmon-initiated I2C writes:

```
Request protocol (pmon → daemon):
  /run/wedge100s/sfp_N_write_req     JSON: {offset, length, data_hex}
  /run/wedge100s/sfp_N_write_ack     Written by daemon: "ok" or "err:<msg>"

Read protocol (pmon ← daemon):
  /run/wedge100s/sfp_N_eeprom        256-byte cache (unchanged — already daemon-owned)
```

**pmon changes (`sfp.py`):**
- `write_eeprom(offset, num_bytes, write_buffer)`: write request file, poll for ack
  file (timeout 5s), raise exception on err or timeout. 5s accounts for worst-case
  timing where the request arrives just after a daemon tick (3s gap + processing).
- `_hardware_read_lower_page()`: migrate live smbus2 DOM reads to a daemon read-request
  file, same pattern as write. This is required to fully eliminate direct i2c-1 access.
- Remove all smbus2 imports and direct `/dev/i2c-1` access from sfp.py once both
  write_eeprom and _hardware_read_lower_page are migrated
- `_eeprom_bus_lock` (RLock) becomes unnecessary — remove

**Daemon changes:**
- Each poll tick: after presence and EEPROM work, scan for `sfp_N_write_req` files
- For each found: perform I2C write (under existing bus ownership), re-read 256 bytes,
  update cache atomically, write ack file, delete request file
- Request files older than 5s without an ack: write `err:timeout`, delete request

**Outcome:** Single I2C bus owner. Bus resets from contention eliminated.
pmon I2C path is entirely file I/O — no kernel device access from pmon container.

---

### Item 4c — Bank-Interleaved DOM Reads

**Current state:** DOM TTL is 10s per port. xcvrd requests for all installed modules
can trigger simultaneous lower-page reads across all ports in a single 10s window,
creating a burst of I2C activity.

**Design:**

Split the 32 ports into two bank-groups aligned with the mux topology. Each bank-group
covers two PCA9548 top-level mux channels (16 ports). Bank-groups alternate which one
is eligible for DOM refresh each tick.

```
Bank-group A (mux 0x70 + 0x71):  ports 0–15  → Ethernet0–Ethernet60   (step 4)
Bank-group B (mux 0x72 + 0x73):  ports 16–31 → Ethernet64–Ethernet124 (step 4)

Tick N   (even): refresh eligible ports in bank-group A if TTL expired
Tick N+1 (odd):  refresh eligible ports in bank-group B if TTL expired
```

**Implementation in sfp.py (`read_eeprom` DOM refresh path):**

```python
_DOM_CACHE_TTL = 20        # seconds — max staleness per port (was 10)
_BANK_GROUP_A  = set(range(0, 16))
_BANK_GROUP_B  = set(range(16, 32))
# Module-level counter in sfp.py, lives within xcvrd process lifetime.
# Not persisted to disk — resets to 0 on xcvrd restart (acceptable).
_tick_counter  = 0

def _dom_refresh_eligible(port_index: int) -> bool:
    in_group_a = port_index < 16
    even_tick  = (_tick_counter % 2 == 0)
    return in_group_a == even_tick
```

A port's DOM is only read if (a) TTL has expired AND (b) it is in the active
bank-group this tick. Result: 16 ports maximum per 10s tick instead of 32, with each
port refreshed at most every 20s.

**I2C efficiency gain:**

Each port requires its own mux channel select on the PCA9548 (ports are on different
channels within the same mux, not selectable in bulk). The saving is in the number
of ports serviced per tick: 16 instead of 32. This is approximately half as many
I2C transactions per DOM refresh cycle regardless of per-port overhead.

**Outcome:** ~50% fewer I2C transactions per DOM refresh cycle. Max DOM staleness
increases from 10s to 20s — acceptable since xcvrd's own alert thresholds operate
on much longer timescales (thermalctld polls every 60s).

---

### Item 4d — BMC In-Band Path Investigation (USB-CDC-Ethernet)

**Status:** Investigation item — not committed to implementation.

**Context:** BMC REST API (Redfish) is not available on this Facebook OpenBMC build.
The current `/dev/ttyACM0` (USB-CDC-ACM) path uses blocking serial I/O because
`O_NONBLOCK` + `select()` is broken for CDC-ACM on kernel 6.1/6.12.

**Hypothesis:** The same USB cable that presents `/dev/ttyACM0` may be configurable
to also enumerate a USB-CDC-ECM (or RNDIS) Ethernet function, creating a `usb0`
network interface on the host with a private /30 link directly to the BMC — no
management switch involved. TCP sockets on this interface support proper non-blocking
I/O and timeout semantics.

**Investigation steps (pre-implementation):**

1. Check `lsusb -v` for the BMC USB device — does it advertise multiple configurations
   or interface alternate settings that include CDC-ECM?
2. On the BMC: `ls /sys/class/udc/` — is a USB Device Controller exposed that could
   be reconfigured via ConfigFS?
3. Check Facebook OpenBMC source for `g_cdc`, `g_ether`, or `usb-gadget` systemd
   service definitions
4. If CDC-ECM is available: assign a private IP (e.g. `169.254.100.1/30` on host,
   `169.254.100.2/30` on BMC), replace TTY session parser with a simple TCP command
   socket or SSH subprocess

**If investigation confirms CDC-ECM is available:** add as a follow-on implementation
item with its own plan. The TTY daemon remains the fallback if the USB-Ethernet path
is unavailable.

**If investigation rules it out:** document the finding in `tests/notes/` and accept
the TTY blocking path as a permanent constraint.

---

### Implementation Order for Section 4

**Investigations first (no code):**
- **4a** — verify on hardware whether PCA9535 INT is wired to a host CPU GPIO.
  Check `/sys/class/gpio/`, ONL platform source, and schematic before writing any
  code. If no GPIO path exists, 4a is closed and removed from the implementation plan.
- **4d** — `lsusb -v` and BMC USB gadget config check. Parallel with 4a investigation.

**Implementation (after investigations resolve 4a scope):**

1. **4c first** (bank interleaving) — pure sfp.py logic change, no C code, immediately
   testable, reduces baseline bus load before other changes
2. **4b second** (full daemon ownership) — eliminates bus contention; builds on the
   cleaned sfp.py from 4c; includes migrating both write_eeprom and
   _hardware_read_lower_page to request/response files
3. **4a third** (INT_L / persistent daemon) — only if GPIO feasibility confirmed;
   C daemon change plus one-shot→persistent architectural transition; safest to
   implement after 4b has stabilized the daemon's role as sole bus owner
4. **4d** (USB-CDC-Ethernet) — implementation only if investigation confirms CDC-ECM
   availability; otherwise document result in tests/notes/ and close

## Out of Scope

- STP/MSTP — not compiled into this SONiC build
- Ethernet100/104/116 optical bring-up — physical blockers (fiber routing, BCM SI
  settings, peer laser); tracked in TODO.md
- Ethernet0/64 dark lane investigation — tracked in TODO.md; Ethernet1 dark-lane
  causes `test_throughput_25g_pair2` to skip rather than fail
- Mgmt SSH/ping intermittent loss — the default `config_db.json` reduces the disruption
  window (VRF pre-configured, BGP container disabled) but full root-cause investigation
  is deferred

---

## Implementation Order

These three sections are independent and can be executed in any order. Recommended
sequence for lowest risk:

1. **Section 3** (report expansion) — pure code addition, no hardware changes, immediate value
2. **Section 1** (default config) — requires image rebuild and re-flash to test fully;
   can be verified by inspecting the generated JSON and checking the installer hook
   before committing to a rebuild
3. **Section 2** (throughput tests) — requires host SSH access and iperf3 availability
   verification on all endpoints before writing hard assertions
