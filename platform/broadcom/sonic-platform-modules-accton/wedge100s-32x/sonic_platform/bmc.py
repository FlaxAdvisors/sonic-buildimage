#!/usr/bin/env python3
"""
sonic_platform/bmc.py — BMC TTY helper for Accton Wedge 100S-32X.

Translates ONL platform_lib.c to Python.  All thermal, fan, and PSU
telemetry is accessed via /dev/ttyACM0 on the host, which connects to
the OpenBMC console at 57600 8N1.

Public API
----------
send_command(cmd)              -> str  | None  -- raw BMC response
file_read_int(path)            -> int  | None  -- cat a file on the BMC
i2cget_byte(bus, addr, reg)    -> int  | None  -- BMC i2cget (byte)
i2cget_word(bus, addr, reg)    -> int  | None  -- BMC i2cget (word)
i2cset_byte(bus, addr, reg, v) -> bool |       -- BMC i2cset (byte)

Design notes
------------
* Open/close cycle per command mirrors bmc_send_command() in
  platform_lib.c (static fd held only for the duration of a call).
* _read_until() replaces the C "usleep then single read" with a proper
  select()-based loop; correct behaviour on slow BMC responses.
* The fd uses blocking I/O with VMIN=1.  ttyACM (USB CDC) does not
  signal select() correctly under O_NONBLOCK on this kernel; blocking
  mode with VMIN=1 is required.  select() still provides timeouts.
* _TTY_PROMPT is b':~# ' (colon-tilde-hash-space), matching the OpenBMC
  root shell prompt "root@HOSTNAME:~# " regardless of hostname.  The
  C code used "@bmc:" which only works when the BMC hostname is "bmc";
  on this target the hostname is "hare-lorax-bmc".
* threading.Lock serialises access within one process; fcntl.flock on
  _FLOCK_PATH serialises across processes (e.g. pmon bmc-poller racing
  with a test or report subprocess calling send_command concurrently).
* The null byte appended to every write mirrors C's
  write(fd, buf, strlen(buf)+1).  It is harmless to the BMC shell.
"""

import fcntl
import os
import select
import termios
import threading
import time

# ---------------------------------------------------------------------------
# Constants (match platform_lib.c)
# ---------------------------------------------------------------------------

# Public API (R29): only send_command() remains after R29 refactor.
# Thermal/fan/PSU reads now come from /run/wedge100s/ daemon files.
# send_command() is retained for fan.set_speed() → 'set_fan_speed.sh <pct>'.

_TTY_DEVICE    = '/dev/ttyACM0'
_FLOCK_PATH    = '/run/wedge100s/bmc.lock'  # cross-process mutex
# OpenBMC root shell prompt: "root@HOSTNAME:~# "
# Using ":~# " matches any root-at-home-dir prompt regardless of hostname.
# The C code uses "@bmc:" which only works when the BMC hostname is literally
# "bmc"; on this system the hostname is "hare-lorax-bmc".
_TTY_PROMPT    = b':~# '
_TTY_RETRY     = 10
_CMD_TIMEOUT   = 5.0   # seconds per attempt (vs C's 60 ms * attempt; generous)
_LOGIN_TIMEOUT = 2.0   # seconds for each login step
_BUF_SIZE      = 1024

# Serialises TTY access within a single Python process.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level TTY helpers
# ---------------------------------------------------------------------------

def _tty_open():
    """
    Open and configure /dev/ttyACM0 as 57600 8N1 raw.
    Retries up to 20 times (mirrors C tty_open's retry loop).
    Returns a file descriptor >= 0, or -1 on failure.
    """
    for _ in range(20):
        try:
            fd = os.open(_TTY_DEVICE, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attr = termios.tcgetattr(fd)
            # c_iflag: ignore parity errors
            attr[0] = termios.IGNPAR
            # c_oflag: no output processing
            attr[1] = 0
            # c_cflag: 57600 | CS8 | CLOCAL | CREAD
            attr[2] = termios.B57600 | termios.CS8 | termios.CLOCAL | termios.CREAD
            # c_lflag: raw (no echo, no canonical, no signals)
            attr[3] = 0
            # c_cc: VMIN=1, VTIME=0 — read returns after the first byte arrives.
            # (C code used VMIN=255 with O_NONBLOCK where it has no effect;
            # we need VMIN=1 here because we use blocking I/O + select.)
            attr[6][termios.VMIN]  = 1
            attr[6][termios.VTIME] = 0
            # baud rate in speed fields as well (mirrors cfset{i,o}speed calls)
            attr[4] = termios.B57600
            attr[5] = termios.B57600
            termios.tcsetattr(fd, termios.TCSANOW, attr)
            # Switch to blocking I/O with VMIN=1 so select() returns on the
            # first byte (VMIN=255 would make select() wait for 255 bytes).
            # Note: ttyACM (USB CDC) does not signal select() correctly in
            # O_NONBLOCK mode on this kernel; blocking mode is required.
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            return fd
        except OSError:
            time.sleep(0.1)
    return -1


def _tty_close(fd):
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _drain(fd, settle=0.05):
    """
    Discard pending input, waiting settle seconds after the last byte
    to ensure any trailing prompts or echoes are fully consumed before
    a new command is written.
    """
    last_read = time.time()
    while True:
        elapsed = time.time() - last_read
        if elapsed >= settle:
            break
        remaining = settle - elapsed
        r, _, _ = select.select([fd], [], [], min(remaining, 0.05))
        if r:
            try:
                os.read(fd, _BUF_SIZE)
                last_read = time.time()   # reset settle timer on each byte burst
            except OSError:
                break


def _read_until(fd, needle, timeout):
    """
    Read from fd, accumulating bytes until needle is found or timeout
    expires.  Returns all accumulated bytes (needle included if found).
    """
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        r, _, _ = select.select([fd], [], [], max(0.0, min(remaining, 0.05)))
        if r:
            try:
                chunk = os.read(fd, _BUF_SIZE)
                if not chunk:
                    break   # EOF / device gone
                buf += chunk
            except OSError:
                break
        if needle in buf:
            break
    return buf


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _tty_login(fd):
    """
    Bring the TTY to the @bmc: prompt.
    Mirrors tty_login() in platform_lib.c.
    Returns True when the prompt is reached.
    """
    for _ in range(_TTY_RETRY):
        # One CR refreshes the prompt; using one (not two as in C) avoids
        # a double-prompt race where the second ":~# " arrives after login
        # returns and is then mistaken for the command response.
        os.write(fd, b'\r\x00')
        buf = _read_until(fd, _TTY_PROMPT, 1.0)
        if _TTY_PROMPT in buf:
            return True

        if b' login:' in buf:    # matches "hostname login:" regardless of hostname
            os.write(fd, b'root\r\x00')
            buf = _read_until(fd, b'Password:', _LOGIN_TIMEOUT)
            if b'Password:' in buf:
                os.write(fd, b'0penBmc\r\x00')
                buf = _read_until(fd, _TTY_PROMPT, _LOGIN_TIMEOUT)
                if _TTY_PROMPT in buf:
                    return True

        time.sleep(0.05)

    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_command(cmd):
    """
    Send a shell command to the BMC and return the full response string.

    cmd   -- command text without trailing newline, e.g. 'cat /proc/uptime'

    Returns the raw response (echo + output + prompt) as a str on
    success, or None after all retries are exhausted.

    Mirrors bmc_send_command() in platform_lib.c.
    """
    with open(_FLOCK_PATH, 'w') as _lf:
        fcntl.flock(_lf, fcntl.LOCK_EX)
        with _lock:
            # Append \r\n to terminate the command line; append \x00 to mirror
            # C's write(fd, buf, strlen(buf)+1) which includes the null byte.
            cmd_bytes = cmd.encode('ascii') + b'\r\n\x00'
            for _attempt in range(1, _TTY_RETRY + 1):
                fd = _tty_open()
                if fd < 0:
                    continue
                try:
                    if not _tty_login(fd):
                        continue
                    _drain(fd)
                    os.write(fd, cmd_bytes)
                    buf = _read_until(fd, _TTY_PROMPT, _CMD_TIMEOUT)
                    if _TTY_PROMPT in buf:
                        return buf.decode('latin-1', errors='replace')
                except OSError:
                    pass
                finally:
                    _tty_close(fd)

    return None


