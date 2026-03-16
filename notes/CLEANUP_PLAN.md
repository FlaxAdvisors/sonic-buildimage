# Project Cleanup & Documentation Consolidation Plan
**Date:** 2026-03-15 | **Status:** DRAFT — awaiting approval before execution

---

## Goal

Produce a comprehensive set of short, precise, authoritative documents and a test
suite that runs cleanly against the hardware without leaving it in a changed state.
All new documents are written for rapid onboarding of additional developers or
support engineers — assume no prior context.

---

## Corrections Found During Pre-Plan Code Review

These are facts that earlier notes got wrong; the current code is the ground truth.

### EEPROM Address
The system EEPROM is at **i2c-40/0x50** (24c64, 8 KiB, behind PCA9548 mux 0x74
channel 6). In SONiC Phase 2 it is accessed via hidraw0 exclusively; the sysfs
path `/sys/bus/i2c/devices/40-0050/eeprom` exists only as a code fallback.

- `eeprom.py` docstring: *"Hardware: 24c64 (8 KiB) at 0x50 on mux 0x74 ch6 (i2c-40)"*
- `i2c_bus_map.json` idprom section: `"bus": 40, "addr": "0x50"`

**False path (for BEWARE doc):** During early development, TlvInfo was written to
the AT24C02 at i2c-1/0x51. That chip is the COME module's internal EEPROM, not the
system EEPROM. The system EEPROM at i2c-40/0x50 was reached only after traversing
the PCA9548 mux; i2c-1/0x50 is the EC chip (silently discards writes).

### `platform_smbus.py` — NOT dead code
`chassis.py` imports it at module level; `sfp.py` imports it in its sysfs fallback
path. It is the CPLD SMBus read helper used by presence detection. Do NOT delete.

### `i2c_bus_map.json` — Stale sections
The `required_kernel_modules` list includes `i2c_mux_pca954x`, `at24`, `optoe` —
these are **not loaded** in the current Phase 2 architecture. The CPLD register
map, mux topology, and BMC sensor paths are still accurate and worth preserving.

---

## New Directory Layout

```
notes/                              ← NEW, at repo root (replaces tests/notes/ for project docs)
  HARDWARE.md                       ← verified hardware topology (≤ 500 lines, split if needed)
  ARCHITECTURE.md                   ← as-built software stack (≤ 500 lines)
  BUILD.md                          ← build and deploy procedures (≤ 500 lines)
  SUBSYSTEMS.md                     ← per-subsystem API reference
  TEST_PLAN.md                      ← test plan scoped to this hardware
  BEWARE_EEPROM.md                  ← EEPROM false paths and lessons learned
  BEWARE_IRQ.md                     ← IRQ / SSH-responsiveness false paths
  BEWARE_OPTICS.md                  ← optical link issues and autoneg limitations
  i2c_topology.json                 ← git mv from device/.../i2c_bus_map.json
  phases/
    STATUS.md                       ← condensed status table (pointer only from tests/STAGED_PHASES.md)
    PF-01_PLAN.md                   ┐
    PF-01_IMPLEMENTATION.md         ├─ Platform Foundation phases (PF-01 through PF-05)
    PF-01_TEST_PLAN.md              ┘
    ... (one triplet per phase)
    PS-01_PLAN.md                   ┐
    PS-01_IMPLEMENTATION.md         ├─ Platform Subsystem phases (PS-01 through PS-07)
    PS-01_TEST_PLAN.md              ┘
    ... (one triplet per phase)
    NF-01_PLAN.md                   ┐
    NF-01_IMPLEMENTATION.md         ├─ Network Feature phases (NF-01 through NF-09)
    NF-01_TEST_PLAN.md              ┘
    ... (one triplet per phase)
    PW-01_PLAN.md                   ┐
    PW-01_IMPLEMENTATION.md         ├─ Pending Work phases (PW-01 through PW-06, IMPLEMENTATION = "pending")
    PW-01_TEST_PLAN.md              ┘

tests/notes/                        ← OLD location; strip to workflow refs + active TODO only
  TODO.md                           ← keep, prune completed items
  [all other .md files]             ← delete after content verified in notes/
```

**500-line rule:** Any file approaching 500 lines is split into logical sub-files
referenced by a table in the parent (e.g., `HARDWARE_THERMAL.md` linked from
`HARDWARE.md`). No exceptions.

---

## Phase Naming Convention

Old arbitrary phase numbers (0–31, R-prefix, EOS-prefix) are retired.
New names are grouped by functional area and numbered within group.

| Group | Prefix | Phases |
|-------|--------|--------|
| Platform Foundation | PF | 01 I2C Topology, 02 CPLD Driver, 03 Platform Init, 04 BMC Daemon, 05 I2C/QSFP Daemon |
| Platform Subsystems | PS | 01 Thermal, 02 Fan, 03 PSU, 04 QSFP/SFP, 05 System EEPROM, 06 LED, 07 Build & Install |
| Network Features | NF | 01 BCM Config, 02 Transceiver Info & DOM, 03 Counters, 04 Link Status, 05 Speed Change, 06 DPB, 07 Autoneg & FEC, 08 Port Channel, 09 LLDP |
| Pending Work | PW | 01 Chassis LED, 02 PSU Telemetry Fix, 03 BGP/L3, 04 Active Optics, 05 Streaming Telemetry, 06 Media Settings |

`notes/phases/STATUS.md` maps each new name to the old phase number for traceability.

---

## Phase A — Write Authoritative Documents

All documents written from code and hardware-verified facts, not from the old notes.
Documents stand alone — when old notes are deleted, new docs must be self-sufficient.

### A1. `notes/HARDWARE.md`

**Purpose:** First stop for any engineer trying to understand the physical platform.
Answers: what chips exist, where they are, how to reach them.

**Sections:**
1. Platform Overview — CPU (x86 Atom C2538), ASIC (BCM56960 Tomahawk), BMC (AST2400 OpenBMC)
2. Console & Management Access — ttyS0 @ 57600 (host), ttyACM0 @ 57600 (BMC to host)
3. I2C Bus Topology
   - i2c-0 (SMBus i801): RTC 0x08, voltage monitor 0x44, ADC 0x48
   - i2c-1 (CP2112 USB-HID): CPLD 0x32; PCA9548 muxes 0x70–0x74 (NOT instantiated in Phase 2)
   - Note: in Phase 2 architecture, i2c-2 through i2c-41 do NOT exist as kernel buses
4. CPLD (i2c-1/0x32, driver: wedge100s_cpld)
   - Registers: 0x00 version major, 0x01 version minor, 0x02 board ID
   - 0x10 PSU presence/pgood (active-low, bits 0/1 PSU1, bits 4/5 PSU2)
   - 0x3e SYS LED1, 0x3f SYS LED2 (encoding: 0=off, 1=red, 2=green, 4=blue, +8=blink)
5. System EEPROM (i2c-40/0x50 via mux 0x74 ch6) — 24c64, 8 KiB, ONIE TLV format
   - CAUTION: i2c-1/0x50 is the EC chip; i2c-1/0x51 is the COME internal EEPROM
   - In Phase 2: daemon reads via hidraw0; sysfs path 40-0050 exists only if mux driver loaded
6. QSFP Cages (32 ports, QSFP28)
   - Presence: PCA9535 at i2c-36/0x22 (ports 0–15) and i2c-37/0x23 (ports 16–31)
   - EEPROM: address 0x50 in each port's mux channel; XOR-1 interleave on presence bits
   - Port-to-bus map (from sfp_bus_index[], 0-indexed): [3,2,5,4,7,6,9,8,...]
   - In Phase 2: all QSFP access via hidraw0 daemon; kernel i2c_mux_pca954x not loaded
7. Thermal Sensors
   - Host: CPU coretemp via `/sys/class/hwmon/hwmon*/temp*_input` (hwmon wildcard)
   - BMC-side (7 TMP75): BMC i2c-3 addrs 0x48–0x4c, BMC i2c-8 addrs 0x48–0x49
   - BMC sensor sysfs: `devices/<bus>/hwmon/*/temp1_input` (NOT lm75 driver path)
8. Fan (5 trays, managed by OpenBMC)
   - Fan board: BMC i2c-8/0x33; `fan<2n-1>_input` = front RPM, `fan<2n>_input` = rear RPM
   - Presence: `fantray_present` sysfs on same device
   - Speed control: `set_fan_speed.sh <pct>` (global, no per-tray)
   - Max RPM: 15400; direction F2B (intake), fixed
9. PSU (2 slots, managed via CPLD + OpenBMC PMBus)
   - Presence/pgood: CPLD reg 0x10 (see §4)
   - PMBus: BMC i2c-7, mux at 0x70 (PSU1 ch2/0x59, PSU2 ch1/0x5a)
   - Key registers: VIN 0x88, IIN 0x89, IOUT 0x8c, POUT 0x96
10. BCM Port Map — 32 ports, ce0–ce31, lane assignments, XOR-1 interleave

Split candidate: if §3 + §6 together exceed 200 lines, extract to `HARDWARE_I2C.md`.

### A2. `notes/ARCHITECTURE.md`

**Purpose:** Explains the layered software design to a developer adding a new feature
or debugging a subsystem. Answers: what runs, in what order, what reads what.

**Sections:**
1. Design Principle — why the kernel I2C stack is bypassed (EEPROM write-attack surface)
2. Kernel Layer
   - Modules loaded: `i2c_dev`, `i2c_i801`, `hid_cp2112`, `wedge100s_cpld`
   - Intentionally NOT loaded: `i2c_mux_pca954x`, `at24`, `optoe`, `lm75`, `i2c_ismt`
   - wedge100s_cpld sysfs: `/sys/bus/i2c/devices/1-0032/{cpld_version,psu*,led_sys*}`
3. hidraw Layer — CP2112 at `/dev/hidraw0`; owned exclusively by wedge100s-i2c-daemon
4. Compiled Daemons
   - `wedge100s-i2c-daemon`: polls hidraw0 every 3 s; writes to `/run/wedge100s/`
     - QSFP presence: `sfp_N_present` (0/1) for N=0..31
     - QSFP EEPROM: `sfp_N_eeprom` (256 bytes, page 0) on insertion
     - System EEPROM: `syseeprom` (8 KiB, full contents) on first boot
   - `wedge100s-bmc-daemon`: polls BMC ttyACM0 every 10 s; writes to `/run/wedge100s/`
     - `thermal_N` (1–7), `fan_N_front_rpm`, `fan_N_rear_rpm`, `fan_N_present`
     - `psu_1_vin`, `psu_1_iin`, `psu_1_iout`, `psu_1_pout` (and psu_2_*)
5. Python Platform API (`sonic_platform/`)
   - `eeprom.py`: reads `/run/wedge100s/syseeprom`; sysfs fallback
   - `sfp.py`: reads `/run/wedge100s/sfp_N_*`; smbus2 fallback (via platform_smbus.py)
   - `chassis.py`: reads CPLD sysfs for presence events; coordinates all objects
   - `fan.py`, `thermal.py`, `psu.py`: read `/run/wedge100s/` daemon cache files
   - `platform_smbus.py`: thread-safe SMBus handle pool; used by chassis.py and sfp.py fallback
6. systemd Timer Units
   - `wedge100s-i2c-poller.service` + `.timer` (3 s interval, OnBootSec=5s)
   - `wedge100s-bmc-poller.service` + `.timer` (10 s interval, OnBootSec=5s)
7. Boot Sequence — init service → daemons (before pmon) → pmon → xcvrd/thermalctld/etc.
8. pmon Daemon Interactions — which pmon daemons call which Python modules
9. Known Limitations — autoneg not active at ASIC level; optical ports blocked (physical)

### A3. `notes/BUILD.md`

**Purpose:** All build and deploy steps in one place. New developer can build and
install from scratch following this document alone.

**Sections:**
1. Prerequisites (Docker daemon, disk space estimate, `make init` submodule setup)
2. `.deb` only build — the usual fast iteration cycle
3. Full ONIE image build
4. Deploy to target (scp + dpkg sequence)
5. Common failures and known-good fixes (solutions only, no investigation narrative)
6. `rules/config.user` variables worth setting for development

### A4. `notes/SUBSYSTEMS.md`

**Purpose:** Per-subsystem reference for developers implementing tests or debugging
platform API behaviour. One section per subsystem; consistent format throughout.

Format per section: Hardware → Driver/Daemon → Python API → Pass Criteria → Known Gaps

Subsystems: System EEPROM, QSFP/SFP, Fan, PSU, Thermal, LED, CPLD, Port Config

Split candidate: if any two subsystems together exceed 200 lines, each gets its own file
linked from SUBSYSTEMS.md as a table of contents.

### A5. `notes/TEST_PLAN.md`

**Purpose:** Describes what we intend to verify and why — independent of how the
pytest runner implements it. Engineers reading this should understand coverage,
gaps, and the test philosophy without reading test code.

**Sections:**
1. Test Philosophy — dynamic discovery, state-restore contract, skip vs. fail, hardware dependencies
2. Subsystem Coverage Table — subsystem / test stage / pass criteria / known gaps
3. Hardware Dependency Matrix — which tests require EOS peer / populated transceivers / PSU load
4. State-Restore Contract — explicit list of what each stage is allowed to change and must restore
5. SONiC Feature Coverage — which SONiC platform features apply to this hardware; which do not
6. Test Report Format — description of `stage_18_report/` output

### A6. `notes/BEWARE_EEPROM.md`

**Purpose:** Must-read before touching any EEPROM-related code. Condenses ~3 months
of painful debugging into a 2-minute briefing.

**Sections:**
1. I2C Address Map — three distinct EEPROMs on the bus and how to tell them apart
   - i2c-1/0x50: EC chip — ACKs writes but silently discards them; do not confuse with EEPROM
   - i2c-1/0x51: COME module internal EEPROM — TlvInfo was mistakenly written here early in dev
   - i2c-40/0x50: TRUE system EEPROM (24c64, 8 KiB, ONIE TLV) — behind mux 0x74 ch6
2. The Write-Attack Surface — how the kernel i2c_mux_pca954x + optoe/at24 stack reaches QSFP 0x50 on every driver probe, dpkg -i, or systemctl restart pmon
3. Corruption Observed — byte 0 overwritten with 0xb3; upper page vendor/PN/SN destroyed; syseeprom data blasted into QSFP EEPROM cell
4. The Fix — wedge100s-i2c-daemon owns /dev/hidraw0; no mux driver, no optoe, no at24
5. The `onie-syseeprom` Trap — verifies from in-memory buffer, not hardware; reports "passed" even when nothing was written
6. Do Not Revert — loading i2c_mux_pca954x will immediately re-trigger probe-writes on any inserted module

### A7. `notes/BEWARE_IRQ.md`

**Sections:**
1. IRQ 18 Overload — I2C presence polling at 800/s caused SSH blackouts; reduced to 69/s by bulk-read batching
2. BCM IRQ Affinity — pinning IRQ 16 (BCM56960) to CPU3 via `isolcpus=3`; commands to verify
3. Boot Gap / TCP Black Hole — daemon startup caused ~15–30 s SSH blackout; root cause and mitigation
4. IRQ Number Instability — dynamic assignment changes between builds; use sysfs proc/irq path, not hardcoded number

### A8. `notes/BEWARE_OPTICS.md`

**Sections:**
1. Ethernet104/108 CWDM4 Modules — link stays DOWN; confirmed physical cable/module issue, not software
2. Autoneg — SONiC CLI accepts config; SAI does NOT program ASIC (`phy_an_c73=0x0`); do not change
3. FEC — RS-FEC (CL91) required for 100GBASE-CR4; FC-FEC (CL74) not supported for 100G on Tomahawk

---

## Phase B — Per-Phase Notes Under `notes/phases/`

### B1. `notes/phases/STATUS.md`

A pure table: new name / old number / description / status / verification date.
`tests/STAGED_PHASES.md` becomes: one-paragraph summary + pointer to this file.

### B2. Per-Phase Triplet Rules

**_PLAN.md** — Written with the perspective of a new developer about to implement this phase:
- Problem statement and motivation
- Proposed approach and files to change
- Acceptance criteria
- Risks and things to watch out for

**_IMPLEMENTATION.md** — Written from code examination, tagged with verification dates:
- What was built (exact files changed)
- Key decisions made and why
- Hardware-verified facts
- Remaining known gaps
- For pending phases: `STATUS: Pending` header, skip implementation body

**_TEST_PLAN.md** — Written with clarity of hindsight, as if the phase is not yet tested:
- What a passing automated test looks like
- Required hardware state (cables, ports, peer, PSU load)
- Step-by-step test actions
- Pass/fail criteria with exact expected values where known
- Mapping to test stage (`stage_NN_*/`)
- State changes the test makes and how it restores them

### B3. Implementation Notes Require Code Examination

Each IMPLEMENTATION.md must be written from reading the current source, not from
old investigation notes. For each phase, examine:

| Phase | Primary files to examine |
|-------|--------------------------|
| PF-01 | `i2c_topology.json` (after git mv), `HARDWARE.md` topology section |
| PF-02 | `modules/wedge100s_cpld.c`, kernel sysfs attributes |
| PF-03 | `utils/accton_wedge100s_util.py`, `debian/postinst` |
| PF-04 | `utils/wedge100s-bmc-daemon.c`, `service/wedge100s-bmc-poller.*` |
| PF-05 | `utils/wedge100s-i2c-daemon.c`, `service/wedge100s-i2c-poller.*` |
| PS-01 | `sonic_platform/thermal.py` |
| PS-02 | `sonic_platform/fan.py` |
| PS-03 | `sonic_platform/psu.py` |
| PS-04 | `sonic_platform/sfp.py`, `sonic_platform/platform_smbus.py` |
| PS-05 | `sonic_platform/eeprom.py` |
| PS-06 | `device/.../plugins/led_control.py`, `sonic_platform/chassis.py` |
| PS-07 | `debian/rules`, `debian/control`, `debian/postinst`, `setup.py` |
| NF-01 | `device/.../th-wedge100s-32x100G.config.bcm`, `th-wedge100s-32x-flex.config.bcm`, `sai.profile` |
| NF-02 | `sonic_platform/sfp.py` transceiver info methods |
| NF-03 | `stage_12_counters/test_counters.py` |
| NF-04 | `stage_13_link/test_link.py` |
| NF-05 | `device/.../platform.json` speed fields |
| NF-06 | `device/.../platform.json`, `device/.../hwsku.json`, `th-wedge100s-32x-flex.config.bcm` |
| NF-07 | `stage_15_autoneg_fec/test_autoneg_fec.py` |
| NF-08 | `stage_16_portchannel/test_portchannel.py` |
| NF-09 | No dedicated test stage yet |

---

## Phase C — i2c_bus_map.json Relocation

```bash
git mv device/accton/x86_64-accton_wedge100s_32x-r0/i2c_bus_map.json \
       notes/i2c_topology.json
```

Add a header comment block to `notes/i2c_topology.json`:

```json
{
  "_NOTICE": [
    "This file is reference documentation only. It is not loaded at runtime.",
    "The CURRENT (Phase 2) kernel module list is: i2c_dev, i2c_i801, hid_cp2112, wedge100s_cpld.",
    "i2c_mux_pca954x, at24, and optoe are intentionally NOT loaded.",
    "Bus numbers i2c-2 through i2c-41 do NOT exist in the running system.",
    "All QSFP EEPROM and system EEPROM access is via /dev/hidraw0 (wedge100s-i2c-daemon).",
    "The mux_tree, qsfp_port_to_bus, and idprom sections describe the PHYSICAL topology",
    "that the daemon navigates via HID reports — not kernel-visible bus numbers."
  ],
  ...
```

Note in `notes/HARDWARE.md` §3: *"The full physical mux topology with address tables is in
`notes/i2c_topology.json`. The kernel-visible I2C surface is intentionally reduced to i2c-0
and i2c-1 only — this is a deliberate design choice to eliminate the write-attack surface
that caused QSFP EEPROM corruption during early bring-up. See BEWARE_EEPROM.md §2–4."*

---

## Phase D — Dead Code and Stale Files

### D1. `platform_smbus.py` — KEEP, NOT dead
Used by `chassis.py` (module-level import for CPLD reads) and `sfp.py` (presence
fallback). Do not delete.

### D2. Legacy plugins audit
Verify each file in `device/.../plugins/` against current pmon_daemon_control.json:
- `eeprom.py` — used by onie-syseeprom on ONIE; keep
- `led_control.py` — used by ledd; keep
- `psuutil.py`, `sfputil.py` — check if superseded by `sonic_platform/` equivalents
  - If SONiC framework routes all calls through `sonic_platform/`, these can be deleted

### D3. `i2c_bus_map.json` at old path
After `git mv`, references in any other file to `device/.../i2c_bus_map.json` should
be updated to `notes/i2c_topology.json` (check CLAUDE.md, test files, CI configs).

---

## Phase E — ONL/EOS Attribution Scrub in Source Code

Comment-only changes. No functional logic altered. Each changed comment should be
a self-contained accurate description of what the code does, not where it came from.

### Python files

| File | Pattern | Replace with |
|------|---------|--------------|
| `bmc.py` | `"Translates ONL platform_lib.c to Python"` | `"Serial BMC polling implementation"` |
| `psu.py` | `"Source: psui.c in ONL (OpenNetworkLinux)"` | remove attribution line |
| `fan.py` | `"Source: fani.c in ONL"`, `"per ONL fani.c"` (×3) | `"verified on hardware"` / remove |
| `sfp.py` | `"Source: sfpi.c in ONL (OpenNetworkLinux)"`, `"(ONL sfpi.c)"` (×2) | `"verified on hardware YYYY-MM-DD"` |
| `thermal.py` | `"source: thermali.c in ONL"`, `"Mirrors ONL's onlp_file_read_int_max()"` | remove attribution |
| `chassis.py` | `"matching the ONL"` | remove clause |
| `accton_wedge100s_util.py` | `"EOS-like hidraw daemon"`, `"EOS PLXcvr architecture"`, `"EOS-LIKE-PLAN.md"`, `"ONL sfpi.c sfp_bus_index[]"` | neutral functional descriptions |

### C files

| File | Pattern | Replace with |
|------|---------|--------------|
| `wedge100s_cpld.c` | `"from ONL ledi.c / psui.c"` | `"verified on hardware 2026-02-25"` |
| `wedge100s-i2c-daemon.c` | `"ONL sfpi.c"` (×3), `"sfp_bus_index[] from ONL sfpi.c"` | `"verified on hardware"` |
| `wedge100s-bmc-daemon.c` | `"from ONL thermali.c / fani.c / psui.c"`, `"mirrors ONL's..."`, `"mirrors bmc.py and ONL"` | drop ONL references, keep functional text |

---

## Phase F — Test Suite

### F1. State-Restore Principle (all stages)
- Discover ports/PSUs/fans dynamically; never hardcode indices
- Save state before modifying anything; restore in pytest `yield` fixture teardown
- `pytest.skip()` if required hardware is absent, not `fail`
- Assert on counts (N/M populated) not specific port identifiers

### F2. New Test Stages

| Stage | Name | What it tests |
|-------|------|---------------|
| `stage_09_cpld/` | CPLD Sysfs | All wedge100s_cpld sysfs entries readable; values in valid range |
| `stage_10_daemon/` | Daemon Health | Both timer units active; cache files exist and are < 30 s old |
| `stage_17_restore/` | State Restore Audit | Run last; verify no stage left the switch in a changed config state |
| `stage_18_report/` | Platform Status Report | Generate `tests/reports/PLATFORM_STATUS_<date>.md` |

### F3. Existing Test Fixes Required

| Stage | Issue | Fix |
|-------|-------|-----|
| `stage_07_qsfp` | Hardcodes port indices | Dynamic discovery of populated ports |
| `stage_11_transceiver` | Fails if no transceivers present | `pytest.skip()` when 0 ports populated |
| `stage_13_link` | References specific peer IPs | Parameterize peer IP via `target.cfg` |
| `stage_14_breakout` | Leaves port in broken-out state on failure | `yield` fixture: restore original breakout mode |
| `stage_16_portchannel` | Modifies PortChannel1 config | Save/restore full LAG config in fixture teardown |

### F4. `stage_18_report/` Output Format
Generates `tests/reports/PLATFORM_STATUS_<date>.md`:
- Hardware inventory table (ports populated, PSUs present, fan trays)
- Subsystem health (daemon status, cache file age, sensor values)
- All-32-port link table (admin / oper / speed / transceiver type)
- EEPROM validity check (magic bytes + CRC)
- Per-stage test summary (pass / fail / skip counts)

---

## Phase G — Old Notes Migration and Deletion

### Delete after content verified in notes/

| File | Content destination |
|------|---------------------|
| `eeprom-address-relocation-research.md` | BEWARE_EEPROM.md |
| `qsfp-eeprom-corruption-investigation.md` | BEWARE_EEPROM.md |
| `qsfp-eeprom-restoration-2026-03-15.md` | BEWARE_EEPROM.md + ARCHITECTURE.md |
| `ssh-responsiveness-2026-03-09.md` | BEWARE_IRQ.md |
| `ssh-responsiveness-2026-03-10.md` | BEWARE_IRQ.md |
| `ssh-responsiveness-2026-03-12.md` | BEWARE_IRQ.md |
| `irq18-refactor-phases1-2.md` | BEWARE_IRQ.md |
| `REFACTOR_HWSUPPORT.md` | BEWARE_IRQ.md |
| `EOS-LIKE-PLAN.md` | ARCHITECTURE.md |
| `eos_i2c_architecture_and_eeprom_plan.md` | ARCHITECTURE.md + BEWARE_EEPROM.md |
| `eos-like-daemon-verification.md` | PF-05 IMPLEMENTATION |
| `eos-p2-hidraw-architecture.md` | PF-05 IMPLEMENTATION + ARCHITECTURE.md |
| `phase-r26-cpld-driver.md` | PF-02 IMPLEMENTATION |
| `phase-r28-bmc-daemon.md` | PF-04 IMPLEMENTATION |
| `phase-r29-python-api-daemon-files.md` | PS-04/PS-05 IMPLEMENTATION |
| `phase-r30-bcm-irq-affinity.md` | BEWARE_IRQ.md |
| `phase-r31-ipmi-rest-investigation.md` | BEWARE_IRQ.md §dead-end (one paragraph) |
| `port_config_lane_verification.md` | HARDWARE.md §10 + NF-01 IMPLEMENTATION |
| `phase-25-active-optics.md` | BEWARE_OPTICS.md + PW-04 PLAN |
| `phase-15-autoneg-fec.md` | BEWARE_OPTICS.md + NF-07 IMPLEMENTATION |
| `dpb-flex-bcm.md` | NF-06 IMPLEMENTATION |
| `phase-14b-dpb.md` | NF-06 IMPLEMENTATION |
| `phase-14a-speed-change.md` | NF-05 IMPLEMENTATION |
| `phase-11-13-interface-verification.md` | NF-02/NF-03/NF-04 IMPLEMENTATION |
| `link-status-investigation.md` | NF-04 IMPLEMENTATION |
| `boot-issues-2026-03-06.md` | BUILD.md §common-failures |
| `lacp-breakout-session-2026-03-06.md` | NF-08 IMPLEMENTATION |
| `stage14-breakout-fixes.md` | NF-06 TEST_PLAN |
| `phase-17-portchannel.md` | NF-08 IMPLEMENTATION |
| `HOWTO-EEPROM.md` | BEWARE_EEPROM.md (superseded) |
| `ARCHSPEC.md` | ARCHITECTURE.md + HARDWARE.md (superseded) |
| `BUILD_GUIDE.md` | BUILD.md (superseded) |

### Delete unconditionally (workflow files no longer needed)
`01-slash-commands.md`, `02-claude-md-guide.md`, `03-prompting-tips.md`, `04-hardware-workflow.md`
(content is covered by Claude Code built-ins and CLAUDE.md)

### Keep permanently
`TODO.md` — prune completed items but keep as active task list

### Shrink, keep in `tests/`
`STAGED_PHASES.md` — one-paragraph project summary + link to `notes/phases/STATUS.md`

---

## Execution Order

```
C  git mv i2c_bus_map.json → notes/i2c_topology.json + add NOTICE header
   (do this first so A1 can reference the new canonical location)
   ↓
A  Write notes/*.md authoritative docs (A1–A8 can be done in parallel)
   ↓
B  Write notes/phases/* per-phase triplets (requires code examination)
   ↓
D  Verify dead code candidates; delete confirmed-dead files
   ↓
E  ONL/EOS attribution scrub (comment-only, no logic changes)
   ↓
F  New test stages + existing test fixes
   ↓
G  Delete old tests/notes/*.md files after verifying content captured
   Shrink tests/STAGED_PHASES.md to pointer
```

Do not delete any note in Phase G until its content destination is confirmed written.

---

## Files Explicitly NOT in Scope

- Functional logic in `sonic_platform/*.py` (comment scrub only)
- `platform.json`, `hwsku.json`, `installer.conf` — correct, in production use
- `wedge100s_cpld.c`, `wedge100s-i2c-daemon.c`, `wedge100s-bmc-daemon.c` — functional, comment scrub only
- All `stage_*/test_*.py` except targeted fixes in F3
- `CLAUDE.md` — minor path update after `notes/` created (i2c_bus_map.json path)
