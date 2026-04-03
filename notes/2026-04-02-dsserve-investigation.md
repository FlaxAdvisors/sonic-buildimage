# dsserve/bcmcmd Investigation — 2026-04-02

## Summary

bcmcmd CLI cannot connect to dsserve after boot because the sai-stat-shim
(our custom SAI counter library loaded inside syncd) holds a persistent
connection to the dsserve Unix socket, and dsserve is a single-client server.

**The standard ledinit pipeline works correctly at boot** — start_led.sh
connects via bcmcmd and loads led_proc_init.soc before the shim takes the
socket.  The problem is only with ad-hoc bcmcmd usage after boot.

## Architecture

```
bcmcmd (CLI)
    |
    +--> connect(/var/run/sswsyncd/sswsyncd.socket)
             |
         dsserve (PID 26, 3 threads)
             |  fd 5: listening socket (inode 23052, backlog=1)
             |  fd 6: accepted client connection (inode 25355)
             |  fd 3: /dev/pts/ptmx  (master side of pty)
             |  fd 4: /dev/pts/2     (slave side, for reference)
             |
             |  thread 26 (main): do_wait — blocked waiting for child (syncd)
             |  thread 48: unix_stream_read_generic — reading from socket fd 6
             |  thread 49: wait_woken — sleeping (output relay thread)
             |
         syncd (PID 47, child of dsserve)
             fd 0,1,2 = /dev/pts/2 (pty slave — stdin/stdout/stderr)
             +-- sai-stat-shim (loaded as SAI library)
                 g_bcmfd = persistent socket to dsserve
```

## dsserve Design (platform/broadcom/sswsyncd/dsserve.cpp)

- Creates a Unix domain socket at `/var/run/sswsyncd/sswsyncd.socket`
- `listen(sockfd, 1)` — backlog of **1**
- Spawns syncd as child with pty as stdio
- Two threads relay data between socket client and pty:
  - `_ds2tty`: accepts one client, reads from socket, writes to pty (syncd stdin)
  - `_tty2ds`: reads from pty (syncd stdout), writes to connected socket client
- Single-client design: `_dsfd` is a global; only one client can be active
- When client disconnects, `_ds2tty` closes the fd and loops back to accept()

## What Happens at Boot (verified from syslog Apr 3 12:52)

1. **12:52:48** — supervisord spawns `ledinit` (start_led.sh)
2. **12:52:53** — bcmcmd polls socket, gets one timeout (syncd still initializing)
3. **12:52:54** — bcmcmd connects successfully, sends `rcload led_proc_init.soc`
4. **12:52:54** — syncd logs: `SAI_API_SWITCH:sai_driver_shell:367 BCM shell command: rcload ...`
5. **12:52:54** — bcmcmd receives `drivshell>` prompt, exits cleanly (status 0)
6. **12:52:59** — sai-stat-shim calls `bcmcmd_connect()`, takes the socket permanently

The timing works because `ledinit` (priority 4) runs before the shim
initializes.  The shim connects ~5 seconds after ledinit finishes.

## Why bcmcmd Fails After Boot

```
$ docker exec syncd timeout 5 bcmcmd -t 1 "echo hello"
polling socket timeout: Success    (exit code 62 = ETIME)
```

The sai-stat-shim (wedge100s-32x/sai-stat-shim/shim.c) holds `g_bcmfd` open
as a persistent connection to dsserve for periodic `show counters` queries.
Since dsserve only accepts one client at a time, new bcmcmd connections cannot
be accepted.  The accept queue (backlog=1) fills up and bcmcmd's `poll()` on
the connected-but-not-accepted socket times out.

Evidence from `ss -lxp` output:
```
u_str LISTEN 1  1  /var/run/sswsyncd/sswsyncd.socket  23052  *  0  users:(("dsserve",pid=26,fd=5))
```
Recv-Q=1, Send-Q=1 means the listen queue is full (one pending unaccepted connection).

Evidence from `/proc/net/unix`:
```
(inode 23052)  State 01 (LISTEN)    — dsserve listening socket
(inode 0)      State 02 (CONNECTING) — stuck pending connection
(inode 25355)  State 03 (CONNECTED)  — shim's persistent connection
```

## Failure Mode During Container Restart (observed Apr 3 00:02)

When syncd container restarts, the timing can go wrong:
```
00:02:18  ledinit: polling socket timeout  (dsserve socket not ready)
00:02:19  ledinit: connecting stream socket: Connection refused
00:02:20  ledinit killed by SIGTERM (container shutdown)
```

On the next restart (00:03:10), ledinit succeeds because the timing works out.
This is a race condition but the retry loop in start_led.sh (`wait_syncd`) handles it.

## Key Files

| File | Role |
|---|---|
| `platform/broadcom/sswsyncd/dsserve.cpp` | Socket server, single-client pty relay |
| `platform/broadcom/sswsyncd/bcmcmd.cpp` | CLI client, connects and sends commands |
| `src/sonic-sairedis/syncd/scripts/syncd_init_common.sh` | Wraps syncd with dsserve |
| `wedge100s-32x/sai-stat-shim/shim.c` | Persistent socket client (g_bcmfd) |
| `wedge100s-32x/sai-stat-shim/bcmcmd_client.c` | Socket client library used by shim |
| Container: `/usr/bin/start_led.sh` | ledinit supervisor program |
| Container: `/usr/share/sonic/platform/led_proc_init.soc` | LED bytecode + remap |

## Conclusions

1. **ledinit/start_led.sh works correctly** — LED bytecodes are loaded at boot
   via bcmcmd before the shim takes the socket.  No fix needed for the boot path.

2. **bcmcmd is blocked after boot** by the sai-stat-shim's persistent socket
   connection.  This is expected behavior given the single-client dsserve design
   and the shim's architecture.

3. **The /dev/mem mmap bypass (read_ledup_mmap.py) remains the correct approach**
   for runtime LED register inspection and manipulation, because:
   - dsserve is architecturally single-client
   - The shim legitimately needs the socket for counter polling
   - Modifying dsserve for multi-client would require upstream changes
   - /dev/mem gives direct hardware register access without socket contention

4. **Possible improvements** (none required):
   - The shim could release and re-acquire the socket between polls (adds latency)
   - dsserve could be patched to support multiple clients (significant complexity)
   - A second diag shell socket could be created (SDK limitation — one diag shell)
   - bcmcmd could detect the shim and signal it to temporarily release the socket

(verified on hardware 2026-04-02, syslog analysis of boot at 12:52 UTC)
