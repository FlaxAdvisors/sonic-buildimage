# SAI Stat Shim — Brainstorming Continuation Prompt

## Context

Working on Accton Wedge100S-32X SONiC port (branch `wedge100s`).
Brainstorming a fix for breakout sub-port counter collection failure.

## Problem Statement

12 flex sub-ports (Ethernet0-3, Ethernet64-67, Ethernet80-83) only populate
2 keys in COUNTERS_DB (`SAI_PORT_STAT_IN/OUT_DROPPED_PKTS`) instead of the
full 66. Non-breakout ports work correctly (68 keys each).

Root cause: `libsaibcm.so 14.3.0.0.0.0.3.0` — `brcm_sai_get_port_stats()`
returns non-SUCCESS for all BCM logical ports in flex `.0` portmap entries:
- BCM ports 50-53 (Ethernet64-67), 68-71 (Ethernet0-3), 118-121 (Ethernet80-83)
Confirmed via syslog: `RID oid:0x100000032 can't provide the statistic` etc.

BCM hardware counters ARE working — `bcmcmd show counters xe36` returns real
RPKT/TPKT/RBYT/TBYT data. The failure is purely in the SAI translation layer.

## What Was Already Tried (In BCM Config)

Both of these are currently active in `th-wedge100s-32x-flex.config.bcm`:
- `bcm_stat_interval=2000000` — confirmed DMA running, no effect on SAI path
- `sai_stats_support_mask=0x1` — changed probe behavior, didn't fix collection

## Key Technical Facts

- `libsai.so.1.0` = 506MB monolithic binary, **zero exported dynamic symbols**
  — `bcm_stat_get()` is NOT accessible via `dlsym()`
- BCM diag shell (bcmcmd) socket IS accessible inside syncd container
- FlexCounter uses two groups for ports:
  - `PORT_BUFFER_DROP_STAT` → 2 drop counters (works, uses different SAI path)
  - `PORT_STAT_COUNTER` → 66 standard counters (broken for flex sub-ports)
- Breakout is **dynamic** — `config interface breakout` can change at runtime
- EOS on same hardware advertises "trident" and has full stats for all ports

## Brainstorming Session Progress

Using superpowers brainstorming skill. Questions answered so far:

- **Q1 (permanence)**: Production component, permanent — unlikely old platform
  gets fixed libsai. Will ship to customers.
- **Q2 (stat coverage)**: All 66 SAI stat IDs needed. Parity with EOS and
  working non-breakout ports. Not just "show interface counters" subset.
- **Q3 (backend)**: bcmcmd socket approach is viable but user raised two
  important additional requirements (see Q4 below).
- **Q4 (investigation first)**: Before committing to shim design, investigate
  whether an alternative `libsaibcm.so` variant exists in the SONiC broadcom
  image that handles flex sub-port stats correctly on Tomahawk silicon.

## Next Steps (In Order)

### Step 1 — Investigation (do this first)
Search the SONiC broadcom syncd image for alternative libsaibcm packages or
variants. EOS identifies this Tomahawk platform using "trident" diag commands
— there may be a td3/Trident3 or alternate Tomahawk variant of libsaibcm.so
that handles flex port stats correctly and can run on BCM56960 silicon.

Commands to run on switch:
```bash
# What's installed
ssh admin@192.168.88.12 "sudo docker exec syncd dpkg -l | grep saibcm"
# Other versions available in apt
ssh admin@192.168.88.12 "sudo docker exec syncd apt-cache show libsaibcm 2>/dev/null | grep -E 'Version|Filename'"
# Check if multiple SAI libs exist for different ASIC families
ssh admin@192.168.88.12 "find /var/cache/apt /usr/lib -name 'libsaibcm*' 2>/dev/null"
# Check what ASIC chip IDs libsai.so.1.0 supports internally
ssh admin@192.168.88.12 "sudo docker exec syncd strings /usr/lib/libsai.so.1.0 | grep -E 'BCM5696|56960|tomahawk|trident|TD3|TH[^2-9]' | head -20"
```

If investigation finds a working alternative → use it instead of shim.
If not → proceed to shim design (Step 2).

### Step 2 — Shim Design (if Step 1 finds nothing)

Continue the brainstorming session with these design requirements established:

**Requirements:**
1. `LD_PRELOAD` library injected into syncd container
2. Intercept point: `sai_api_query(SAI_API_PORT)` → replace `get_port_stats`
   and `get_port_stats_ext` function pointers in the returned struct in-place
3. **Transparent/dynamic**: detect flex sub-ports at SAI call time via
   `sai_get_port_attribute(SAI_PORT_ATTR_HW_LANE_LIST)` cross-referenced
   against portmap entries — NOT a hardcoded port list
4. Stat collection backend: bcmcmd socket (text protocol, parsed)
5. Coverage: all 66 `PORT_STAT_COUNTER` IDs + passthrough for non-flex ports
6. Non-breakout ports: pure passthrough to original `libsaibcm.so` functions
7. Permanent production component — needs robust error handling

**Remaining brainstorming questions to ask:**
- How should the shim handle the bcmcmd socket latency (batch all flex ports
  in one call vs. per-port calls)?
- Where does it live in the build/package system? Platform .deb postinst
  installs it + patches syncd supervisor config?
- What happens if the bcmcmd socket is unavailable (syncd startup race)?
- SAI→BCM stat ID mapping: use public BCM SDK headers or derive empirically?

## File Locations

- BCM config: `device/accton/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm`
- Platform modules: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/`
- Test suite: `tests/`
- Notes: `notes/`

## Continuation Prompt

To continue: invoke the brainstorming skill, state we are mid-session on the
SAI stat shim design for Wedge100S, reference this file for full context,
then execute Step 1 (alternative libsaibcm investigation) and proceed from
there per the steps above.
