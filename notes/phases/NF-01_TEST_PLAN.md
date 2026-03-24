# NF-01 — BCM Config: TEST PLAN

## Mapping

Test stage: `tests/stage_13_link/` (link-up verification depends on BCM config correctness).
No dedicated stage_NN for BCM config alone — it is a prerequisite verified implicitly by
syncd init and port link-up tests.

## Required Hardware State

- SONiC switch running with platform .deb installed
- syncd container running
- Peer device (rabbit-lorax, Arista EOS) connected on at least one 100G DAC port
  (Ethernet16, 32, 48, or 112)

## Step-by-Step Test Actions

### 1. Verify sai.profile selects flex config

```bash
ssh admin@192.168.88.12 cat /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/sai.profile
```

**Pass**: output contains `SAI_INIT_CONFIG_FILE=...th-wedge100s-32x-flex.config.bcm`

### 2. Verify flex config file is present

```bash
ssh admin@192.168.88.12 ls -la /usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/th-wedge100s-32x-flex.config.bcm
```

**Pass**: file exists, size > 10000 bytes

### 3. Verify syncd initialized all 32 ports

```bash
ssh admin@192.168.88.12 redis-cli -n 2 hgetall COUNTERS_PORT_NAME_MAP | grep -c Ethernet
```

**Pass**: count >= 32

### 4. Verify all 32 ports appear in show interfaces status

```bash
ssh admin@192.168.88.12 show interfaces status | grep -c Ethernet
```

**Pass**: count >= 32

### 5. Verify BCM ASIC port state for admin-up connected ports

```bash
ssh admin@192.168.88.12 sudo docker exec syncd bcmcmd "ps" 2>/dev/null | grep "100G"
```

**Pass**: at least 4 lines showing `100G FD ... KR4`

### 6. Verify port speed is 100G for connected ports

```bash
ssh admin@192.168.88.12 show interfaces status Ethernet16 Ethernet32 Ethernet48 Ethernet112
```

**Pass criteria per port:**
- Speed column: `100G`
- FEC column: `rs`
- Admin column: `up`
- Oper column: `up` (requires peer connected and RS-FEC set)

### 7. Verify no critical BCM errors in syncd log

```bash
ssh admin@192.168.88.12 docker logs syncd 2>&1 | grep -i "error\|fatal\|abrt" | grep -v "sai_api_query failed for\|ngknet_dev_ver\|OneSync fw init\|VFP/EFP"
```

**Pass**: no output (the four excluded patterns are known non-fatal)

### 8. Verify lane polarity (XOR-1 interleave) — presence detection sanity

```bash
ssh admin@192.168.88.12 python3 -c "
from sonic_platform.platform import Platform
p = Platform()
chassis = p.get_chassis()
for i in range(32):
    sfp = chassis.get_sfp(i)
    print(f'Port {i+1}: present={sfp.get_presence()}')
"
```

**Pass**: at least 4 ports report `present=True` (the 4 installed DAC cables)

## Pass/Fail Criteria — Summary

| Check | Expected Value | Fail Action |
|---|---|---|
| sai.profile config path | `th-wedge100s-32x-flex.config.bcm` | Check .deb installation |
| COUNTERS_PORT_NAME_MAP entries | >= 32 | Check syncd logs for BCM init error |
| `show interfaces status` rows | >= 32 | BCM config portmap may have errors |
| Connected port speed | `100G` | portmap lane count wrong |
| Connected port oper state | `up` | RS-FEC not set; check NF-04 |
| syncd fatal errors | none (beyond known non-fatals) | BCM config parameter error |

## State Changes and Restoration

This test is read-only — no configuration changes are made. The BCM config is loaded at
syncd init and cannot be changed at runtime without a syncd restart. No teardown required.

## Notes

- The `th-wedge100s-32x100G.config.bcm` path referenced in older notes does not exist in
  the current repo. Only the flex config is present.
- BCM `ps` requires `sudo` inside the syncd container. The test uses
  `docker exec syncd bcmcmd "ps"` from the admin shell.
- SAI warnings `sai_api_query failed for 24 apis` and `ngknet_dev_ver_check: IOCTL failed`
  appear in every syncd log on this platform and are not failures.
