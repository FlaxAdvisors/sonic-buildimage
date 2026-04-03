# Continuation: Replace bcmcmd Counter Path with /dev/mem Direct Register Access

## Context for Next Session

The sai-stat-shim currently fetches flex sub-port (4x25G breakout) counters by connecting to the bcmcmd diag shell socket (`/var/run/sswsyncd/sswsyncd.socket`), sending `show counters`, and parsing text output. This path has three critical problems discovered in the 2026-04-03 session:

1. **Socket contention** — the dsserve socket (backlog=1) blocks other bcmcmd users when the shim holds a connection
2. **3-second banner timeout** — each connect-on-demand attempt blocks for up to 3s waiting for `drivshell>` prompt, causing orchagent SIGABRT when accumulated across concurrent stat calls
3. **Diag shell lifetime** — the BCM diag shell becomes unresponsive ~60s after syncd init completes, making bcmcmd unreachable at runtime

A connect backoff (SHIM_CONNECT_BACKOFF_MS=5000) was added as a mitigation, but the fundamental fix is to bypass bcmcmd entirely and read hardware counters directly via /dev/mem mapped to the Tomahawk's PCIe BAR.

## Brainstorm Topic

**Replace the bcmcmd_client.c socket path with direct Memory-Mapped I/O counter reads from the BCM56960 Memory-Mapped Counter DMA (sobdma) registers via /dev/mem + mmap.**

### What We Know

- **BAR0** is the primary register space for BCM56960 (Memory-Mapped I/O)
- BAR2 (CMIC) is where LEDUP registers live (confirmed at 0x20000-0x21fff) — this is a different region
- The shim already has `/dev/mem` access precedent via `wedge100s_ledup.py` (BAR2 mmap for LED registers)
- The BCM SDK's `sobdma` (SOC Bus DMA) mechanism reads hardware MIB counters
- Counter registers are per-port, at fixed offsets within the sobdma block
- The BCM SDK source (memory permitting) has the register map in `sobdma.h` or `sobdmacmd.c`
- The `show counters` bcmcmd command internally reads these same registers — we just need to do it directly

### Key Questions to Investigate

1. **What BAR contains the sobdma/MIB counter registers?** Likely BAR0 but needs confirmation. Check:
   - `lspci -v -s 06:00.0` on target for BAR addresses and sizes
   - `/proc/iomem` for BCM56960 memory regions
   - BCM SDK source for `soc_counter_*` register definitions

2. **What is the register layout for per-port MIB counters?** Need:
   - Base offset of counter block within the BAR
   - Per-port stride (spacing between port counter blocks)
   - Register offsets for each counter type (RPKT, RBYT, TPKT, TBYT, etc.)
   - Mapping from SDK port number to physical port register index
   - Whether counters are 32-bit or 64-bit (likely 64-bit on TH)

3. **Are counters latched or free-running?** Some ASIC counter architectures require writing a "snapshot" trigger register before reading, to get consistent multi-register reads. Others are free-running and can be read any time.

4. **Do we need sobdma or can we read registers directly?** The sobdma mechanism is the SDK's DMA-based counter collection. Direct register reads (MMIO) may work without DMA setup, but need to confirm the counters are accessible via simple register reads.

5. **Port numbering:** The shim's `g_ps_map` maps SDK port numbers to port names. We need to map SDK port numbers to physical register block indices. The BCM config file (`th-wedge100s-32x-flex.config.bcm`) has `portmap_<sdk_port>.0=<physical_lane>:<speed>` which may give us the physical port index.

### Proposed Architecture

```
shim.c refresh_cache()
  ├── OLD: bcmcmd_connect() → write "show counters\n" → parse text → close
  └── NEW: mmap BAR0 → read counter registers directly → populate cache
```

The new path would:
1. On first call: `open("/dev/mem")`, `mmap()` the BAR0 region, build a physical_port→register_offset table
2. On each `refresh_cache()`: for each port in `g_ps_map`, read the counter registers from the mapped memory
3. No socket, no text parsing, no timeout, no blocking — pure memory reads (~microseconds)

### Starting Points for Investigation

```bash
# On target: find BAR0 address and size
ssh admin@192.168.88.12 'sudo lspci -v -s 06:00.0 2>&1'
ssh admin@192.168.88.12 'sudo cat /proc/iomem | grep -i broadcom'

# In BCM SDK source (if available in the build container):
# Look for sobdma register definitions
grep -r 'sobdma\|SOC_COUNTER_\|MEMORY_MAPPED_COUNTER' /path/to/bcmsdk/

# In the build container, check headers:
# soc/sobdma.h, soc/counter.h, soc/mcm/allenum.h

# On target: try reading a known counter register via devmem2
# (need to know the exact offset first)
ssh admin@192.168.88.12 'sudo docker exec syncd devmem2 0x<bar0_base + counter_offset> w'
```

### Risk Assessment

- **Low risk:** Read-only access to counter registers; no write operations
- **No ASIC state change:** Counter reads don't affect forwarding or configuration
- **Compatibility:** BAR0 address may change between boots (PCI enumeration), so must read it from sysfs/lspci at runtime
- **Correctness:** Need to verify counter values match `show counters` output for validation

### Files That Would Change

| File | Change |
|------|--------|
| `sai-stat-shim/shim.h` | Add `SHIM_BAR0_*` defines, counter register offsets |
| `sai-stat-shim/shim.c` | Replace `refresh_cache()` with mmap-based counter reads |
| `sai-stat-shim/bcmcmd_client.c` | Keep for `bcmcmd_ps()` only (port enumeration), or replace that too |
| `sai-stat-shim/counter_regs.c` | New file: BAR0 mmap setup + per-port counter read functions |

### Session Summary (2026-04-03)

Commits this session:
```
b244fc3 refactor(shim): remove SHIM_CACHE_TTL_MS and fetch_in_progress from header
24c1c3e refactor(shim): connect-on-demand instead of persistent bcmcmd socket
d69a507 feat(bmc-daemon): add led_ctrl_write and led_color_read dispatch
b010f15 feat(led-diag): add SONiC-side LED diagnostic tool via bmc-daemon
d08990c fix(ledup): correct BAR2 register offsets from iProc to CMIC space
921d38a fix(led-diag-bmc): add inotify coalescing workaround with retry logic
6ace9b9 docs: LED pipeline investigation findings (2026-04-03)
4398bf7 fix(shim): add connect backoff to prevent orchagent SIGABRT timeouts
243b7f3 test(led-diag): add stage 27 — CPLD LED pattern write/readback
4e16e85 fix(test): add sudo and inotify retry to LED diag tests
de8cc4e docs: add LED diagnostic operator guide for visual verification
```

Test results at session end:
- stage_27_led_diag: **11/11 passed**
- stage_25_shim: **7/11 passed** (4 failures: DPB timeout, link-down, pre-existing)
- stage_24_counters: **7/10 passed** (3 failures: counter poll timing, link-down)

Key finding: orchagent crashes (SIGABRT) were caused by 3s bcmcmd banner timeouts blocking syncd threads. Mitigated with connect backoff but the real fix is eliminating the socket path entirely.
