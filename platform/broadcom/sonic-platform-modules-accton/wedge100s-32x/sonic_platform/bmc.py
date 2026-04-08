#!/usr/bin/env python3
"""
sonic_platform/bmc.py — BMC communication helper for Accton Wedge 100S-32X.

Primary path: SSH over USB-CDC-Ethernet (usb0).
  BMC usb0 MAC 02:00:00:00:00:01 → IPv6 link-local fe80::ff:fe00:1%usb0
  Switch usb0 MAC 02:00:00:00:00:02 → IPv6 link-local fe80::ff:fe00:2%usb0
  Both addresses are auto-assigned from the MAC; no IP configuration needed
  beyond bringing usb0 UP.  Fast and non-blocking; no TTY protocol overhead.

  Key: /etc/sonic/wedge100s-bmc-key (ed25519, generated at postinst time).
  Public key is provisioned to BMC /home/root/.ssh/authorized_keys via TTY
  once at postinst, then persisted to /mnt/data/etc/authorized_keys for
  BMC reboot survival.  After provisioning all runtime calls use SSH only.

Fallback: None is returned when SSH fails.  Callers must handle None
gracefully (return cached/default values).  The blocking TTY path is NOT
used at runtime to avoid the 140-second login-timeout risk.

Public API
----------
send_command(cmd)              -> str  | None  -- raw BMC response (SSH only)
file_read_int(path)            -> int  | None  -- cat a file on the BMC
i2cget_byte(bus, addr, reg)    -> int  | None  -- BMC i2cget (byte)
i2cget_word(bus, addr, reg)    -> int  | None  -- BMC i2cget (word)
i2cset_byte(bus, addr, reg, v) -> bool         -- BMC i2cset (byte)

Provisioning API (postinst only)
---------------------------------
provision_ssh_key()  -- generate key pair, push pubkey to BMC via TTY,
                        persist to /mnt/data/etc/authorized_keys

Design notes
------------
* SSH target uses IPv6 link-local with %usb0 zone ID.  subprocess.run passes
  this literally to ssh; OpenSSH handles the zone ID correctly on Linux.
* Thread-safety: each SSH call spawns an independent subprocess; no shared
  state, so no lock is needed for the SSH path.
* TTY helpers are retained for provision_ssh_key() only.  They are NOT called
  from send_command() and therefore cannot cause runtime latency spikes.
* _read_until() uses select() for timeouts; blocking I/O with VMIN=1 is
  required because ttyACM (USB CDC) does not signal select() correctly under
  O_NONBLOCK on this kernel.
"""

import fcntl
import os
import select
import termios
import threading
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TTY_DEVICE    = '/dev/ttyACM0'
_TTY_PROMPT    = b':~# '          # matches "root@HOSTNAME:~# " regardless of hostname
_TTY_RETRY     = 10
_CMD_TIMEOUT   = 5.0
_LOGIN_TIMEOUT = 2.0
_BUF_SIZE      = 1024

# Serialises TTY access within a single Python process (provision_ssh_key only).
_lock = threading.Lock()

# USB-CDC-Ethernet SSH path.
# IPv6 link-local addresses are derived from the fixed MAC addresses:
#   BMC  usb0 MAC 02:00:00:00:00:01 → fe80::ff:fe00:1
#   Switch usb0 MAC 02:00:00:00:00:02 → fe80::ff:fe00:2
# No IP configuration is needed; the addresses auto-configure when usb0 is UP.
_BMC_SSH_TARGET = 'root@fe80::ff:fe00:1%usb0'
_SSH_TIMEOUT    = 5.0
_SSH_KEY        = '/etc/sonic/wedge100s-bmc-key'


# ---------------------------------------------------------------------------
# Low-level TTY helpers (provisioning use only)
# ---------------------------------------------------------------------------

def _tty_open():
    """Open /dev/ttyACM0 at 57600 8N1 for BMC serial console access.

    Retries up to 20 times with 100 ms delay.  Used for provisioning only.

    Returns:
        int: Open file descriptor on success, -1 on failure.
    """
    for _ in range(20):
        try:
            fd = os.open(_TTY_DEVICE, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attr = termios.tcgetattr(fd)
            attr[0] = termios.IGNPAR
            attr[1] = 0
            attr[2] = termios.B57600 | termios.CS8 | termios.CLOCAL | termios.CREAD
            attr[3] = 0
            attr[6][termios.VMIN]  = 1
            attr[6][termios.VTIME] = 0
            attr[4] = termios.B57600
            attr[5] = termios.B57600
            termios.tcsetattr(fd, termios.TCSANOW, attr)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            return fd
        except OSError:
            time.sleep(0.1)
    return -1


def _tty_close(fd):
    """Close a TTY file descriptor, ignoring errors.

    Args:
        fd: File descriptor returned by _tty_open(), or -1 (no-op).
    """
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _drain(fd, settle=0.05):
    """Read and discard all pending TTY input until the line is quiet.

    Args:
        fd: TTY file descriptor.
        settle: Idle time in seconds after which the drain completes.
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
                last_read = time.time()
            except OSError:
                break


def _read_until(fd, needle, timeout):
    """Read from TTY until needle is found or timeout expires.

    Args:
        fd: TTY file descriptor.
        needle: Byte sequence to search for (bytes).
        timeout: Maximum wait time in seconds.

    Returns:
        bytes: All data read (may or may not contain needle).
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
                    break
                buf += chunk
            except OSError:
                break
        if needle in buf:
            break
    return buf


def _tty_login(fd):
    """Attempt to reach the BMC shell prompt via TTY login sequence.

    Sends CR; if a login prompt appears, logs in with root / 0penBmc.
    Retries up to _TTY_RETRY times.

    Args:
        fd: Open TTY file descriptor.

    Returns:
        bool: True if the shell prompt was reached, False on timeout.
    """
    for _ in range(_TTY_RETRY):
        os.write(fd, b'\r\x00')
        buf = _read_until(fd, _TTY_PROMPT, 1.0)
        if _TTY_PROMPT in buf:
            return True
        if b' login:' in buf:
            os.write(fd, b'root\r\x00')
            buf = _read_until(fd, b'Password:', _LOGIN_TIMEOUT)
            if b'Password:' in buf:
                os.write(fd, b'0penBmc\r\x00')
                buf = _read_until(fd, _TTY_PROMPT, _LOGIN_TIMEOUT)
                if _TTY_PROMPT in buf:
                    return True
        time.sleep(0.05)
    return False


def _tty_send_raw(cmd):
    """Send a command to the BMC via TTY. Returns response str or None.

    FOR PROVISIONING USE ONLY — not called from send_command().
    """
    cmd_bytes = cmd.encode('ascii') + b'\r\n\x00'
    with _lock:
        for _ in range(1, _TTY_RETRY + 1):
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


# ---------------------------------------------------------------------------
# SSH fast path
# ---------------------------------------------------------------------------

def _ssh_send_command(cmd):
    """Send a shell command to the BMC via SSH over USB-CDC-Ethernet.

    Returns stdout as str on success, None on any error.
    Thread-safe: each call is an independent subprocess.
    """
    import subprocess
    try:
        result = subprocess.run(
            [
                'ssh',
                '-i', _SSH_KEY,
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'ConnectTimeout={:d}'.format(int(_SSH_TIMEOUT)),
                '-o', 'BatchMode=yes',
                _BMC_SSH_TARGET,
                cmd,
            ],
            capture_output=True,
            timeout=_SSH_TIMEOUT + 2,
        )
        if result.returncode == 0:
            return result.stdout.decode('latin-1', errors='replace')
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Provisioning (postinst use only)
# ---------------------------------------------------------------------------

def provision_ssh_key():
    """Generate a platform SSH key pair and provision it to the BMC via TTY.

    Called from postinst on every package install/upgrade.  Idempotent:
    skips key generation if the key already exists; always re-provisions
    the pubkey to the BMC so upgrades replace any stale key.

    Key locations:
      Private: /etc/sonic/wedge100s-bmc-key       (accessible in pmon: /etc/sonic is mounted)
      Public:  /etc/sonic/wedge100s-bmc-key.pub
    BMC locations:
      Runtime:    /home/root/.ssh/authorized_keys   (RAM, lost on reboot)
      Persistent: /mnt/data/etc/authorized_keys     (jffs2, survives reboot)
    """
    import subprocess
    key_path = _SSH_KEY
    pub_path = key_path + '.pub'

    os.makedirs(os.path.dirname(key_path), mode=0o755, exist_ok=True)
    if not os.path.exists(key_path):
        subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', key_path],
            check=True, capture_output=True,
        )

    try:
        with open(pub_path) as f:
            pubkey = f.read().strip()
    except OSError:
        return False

    # Provision to BMC via TTY.  Uses shell idiom to avoid duplicates.
    cmd = (
        'mkdir -p /home/root/.ssh && '
        'chmod 700 /home/root/.ssh && '
        "grep -qxF '{pk}' /home/root/.ssh/authorized_keys 2>/dev/null || "
        "echo '{pk}' >> /home/root/.ssh/authorized_keys && "
        'chmod 600 /home/root/.ssh/authorized_keys && '
        'mkdir -p /mnt/data/etc && '
        'cp /home/root/.ssh/authorized_keys /mnt/data/etc/authorized_keys'
    ).format(pk=pubkey)

    result = _tty_send_raw(cmd)
    return result is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_command(cmd):
    """Send a shell command to the BMC and return the response string.

    Uses SSH over USB-CDC-Ethernet only.  Returns None if SSH is unavailable
    (usb0 down, key not provisioned, BMC unreachable).  Callers must handle
    None gracefully — do NOT call this in a tight loop without caching.

    cmd -- command text without trailing newline, e.g. 'cat /proc/uptime'
    """
    return _ssh_send_command(cmd)


def file_read_int(path):
    """cat a file on the BMC and return its integer value, or None."""
    out = send_command('cat ' + path)
    if out is None:
        return None
    try:
        return int(out.strip().split()[0])
    except (ValueError, IndexError):
        return None


def i2cget_byte(bus, addr, reg):
    """Run i2cget -y <bus> <addr> <reg> b on the BMC. Returns int or None."""
    out = send_command('i2cget -y {:d} {:#x} {:#x} b'.format(bus, addr, reg))
    if out is None:
        return None
    try:
        return int(out.strip().split()[-1], 16)
    except (ValueError, IndexError):
        return None


def i2cget_word(bus, addr, reg):
    """Run i2cget -y <bus> <addr> <reg> w on the BMC. Returns int or None."""
    out = send_command('i2cget -y {:d} {:#x} {:#x} w'.format(bus, addr, reg))
    if out is None:
        return None
    try:
        return int(out.strip().split()[-1], 16)
    except (ValueError, IndexError):
        return None


def i2cset_byte(bus, addr, reg, value):
    """Run i2cset -y <bus> <addr> <reg> <val> b on the BMC. Returns bool."""
    out = send_command(
        'i2cset -y {:d} {:#x} {:#x} {:#x} b'.format(bus, addr, reg, value))
    return out is not None
