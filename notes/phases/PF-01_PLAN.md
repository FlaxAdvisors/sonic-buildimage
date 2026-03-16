# PF-01 — I2C Topology: Plan

## Problem Statement

Before writing any driver or platform code for the Wedge 100S-32X, you need a
single authoritative document of the physical I2C topology: which buses exist,
which mux chips route to which downstream buses, what devices live at what
addresses, and which things are on the host versus on the BMC.

Without this map:
- Driver registration order is guesswork (PCA9548 bus numbering is sequential
  and depends on the order mux devices are registered).
- Register addresses for CPLD, PSU PMBus, QSFP presence chips, and system EEPROM
  are scattered across ONL source files in multiple languages.
- The split between host-accessible I2C (CP2112 bridge) and BMC-side I2C
  (TMP75 thermal sensors, fan board, PSU mux) is not documented anywhere
  in the SONiC repo for this platform.

## Proposed Approach

1. Boot the switch into a known-good environment (ONL or early SONiC).
2. Use `i2cdetect -l` to enumerate adapters and `i2cdetect -y -r <bus>` to scan
   each bus non-destructively.
3. Cross-reference the ONL source tree:
   - `packages/platforms/accton/x86-64/wedge100s-32x/onlp/builds/src/`
   - Key files: `sfpi.c` (QSFP bus map), `psui.c` (PMBus mux), `thermali.c`
     (TMP75 addresses), `fani.c` (fan board), `ledi.c` (CPLD LED registers)
4. Capture BMC-side topology over `/dev/ttyACM0` by `cat`-ing sysfs paths.
5. Write the result to `notes/i2c_topology.json` with verified live values.

### Files to Create

- `notes/i2c_topology.json` — canonical machine-readable topology
- `notes/HARDWARE.md` — human-readable reference (buses, CPLD, PSU, QSFP summary)

### Files to Read (ONL reference)

- `/export/sonic/OpenNetworkLinux/packages/platforms/accton/x86-64/wedge100s-32x/`

## Acceptance Criteria

- JSON file exists with: `root_buses`, `cpld` (with register map), `mux_tree`
  (5× PCA9548 with channel-to-bus assignments), `idprom`, `qsfp_presence`,
  `qsfp_port_to_bus` (all 32 ports), `bmc` (thermal, fan, PSU side)
- CPLD version register reads back a known-good value (major=2, minor=6)
- System EEPROM TLV magic verified (`TlvInfo\x00`)
- PSU presence register live value documented
- Port-to-bus map confirmed for at least one occupied QSFP cage

## Risks and Watchpoints

**Mux enumeration order matters.** The Linux `i2c_mux_pca954x` driver assigns
bus numbers sequentially as mux devices are registered. If the five PCA9548 chips
are not registered in address order (0x70, 0x71, 0x72, 0x73, 0x74), the bus
numbers will differ from what ONL documents. Always register 0x70 first.

**BMC-side vs host-side confusion.** The TMP75 thermal sensors, the fan board
at BMC bus 8 / 0x33, and the PSU PMBus mux at BMC bus 7 / 0x70 are on the
*BMC's* I2C buses — not the host's. Any attempt to use `i2cdetect` from the
host on these will either find nothing or corrupt state.

**QSFP EEPROM is write-vulnerable.** Loading `i2c_mux_pca954x` causes the
kernel to probe address 0x50 on every mux channel. Some QSFP EEPROMs are
writable and respond to probe writes with side effects. Minimise the time
`i2c_mux_pca954x` is loaded during topology discovery.

**Bus 40 vs bus 36/37 for mux 0x74.** The system EEPROM (24c64 at 0x50) is on
mux 0x74 channel 6 (logical bus 40). QSFP presence chips are on channels 2 and 3
(logical buses 36/37). These are not the same bus — do not confuse them.

**`required_kernel_modules` section becomes stale.** The JSON was written
before the Phase 2 (hidraw-direct) architecture decision. The `required_kernel_modules`
array reflects the Phase 1 module list; Phase 2 intentionally removes
`i2c_mux_pca954x`, `at24`, and `optoe`. The `_NOTICE` header in the JSON
documents this.
