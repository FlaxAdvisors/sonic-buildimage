# Stage 14 Breakout Test Fixes

## Problems Fixed (2026-03-06)

### 1. Wrong CLI syntax (user error)
`sudo config interface breakout Ethernet80 speed 25G` is INVALID.
Correct syntax: `sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l`
The mode string is a key from `platform.json`, not a speed argument.

### 2. platform.json mode keys wrong
**Before**: `"4x25G"` and `"4x10G"` (4 modes per port)
**After**: `"4x25G[10G]"` only (3 modes: `1x100G[40G]`, `2x50G`, `4x25G[10G]`)

Fixed by running python3 to rename keys in-place. Stage 14 tests expect
`EXPECTED_BREAKOUT_MODES = {"1x100G[40G]", "2x50G", "4x25G[10G]"}`.

### 3. Test paths wrong
Tests were reading from `/usr/share/sonic/platform/platform.json` but
`/usr/share/sonic/platform` symlink does not exist on this platform.
Actual paths:
- `platform.json`: `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/platform.json`
- `hwsku.json`: `/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/hwsku.json`

### 4. SSH instability — paramiko channels not closed
When paramiko `exec_command(cmd, timeout=N)` was used, the timeout applied
to `open_session()`. Under load, channel opens timed out and the session
broke without reconnecting. Fixes to `tests/lib/ssh_client.py`:
- `exec_command(cmd, timeout=None)` — no open_session timeout
- `stdout.channel.settimeout(timeout)` for read timeouts
- Explicit `stdin.close()` and `stdout.channel.close()` in finally
- Reconnect on `SSHException`/`EOFError`/`AttributeError`
- `connect(retries=5, retry_delay=10)` — retry loop in connect()
- `connect_timeout = 30` in `target.cfg`

### 5. SSH intermittent blocking — BCM ASIC interrupts
Root cause: BCM56960 fires ~150 hardware interrupts/sec (normal for BCM
timer interrupt). This causes HI softirq flooding on one CPU, creating
15-30 second windows where sshd cannot accept new TCP connections.
- NOT caused by pmon/xcvrd or GPIO presence detection
- NOT caused by breakout sub-ports or orphaned TCP connections
- IS the normal BCM SDK behavior; workarounds are retry logic in tests

## Verification (verified on hardware 2026-03-06)
```
18 passed in 247.92 seconds
```
All 18 stage 14 tests pass with pmon running.

## Correct DPB workflow
```bash
# Show available modes
show interfaces breakout

# Break out a port (requires flex BCM config — see dpb-flex-bcm.md)
sudo config interface breakout Ethernet80 '4x25G[10G]' -y -f -l

# Revert
sudo config interface breakout Ethernet80 '1x100G[40G]' -y -f -l
sudo config save
```
