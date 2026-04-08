#!/usr/bin/env python3
"""wedge100s-led-diag-bmc.py -- SONiC-side LED diagnostic tool (via bmc-daemon).

Exercises CPLD LED control registers by sending commands through the
wedge100s-bmc-daemon's .set file dispatch. Reads back actual values via
the daemon's /run/wedge100s/ output files. Reports intended vs actual
to verify both the SONiC→BMC communication path and CPLD register writes.

Usage:
    wedge100s-led-diag-bmc.py status
    wedge100s-led-diag-bmc.py set rainbow
    wedge100s-led-diag-bmc.py set solid <0-3>
    wedge100s-led-diag-bmc.py set walk
    wedge100s-led-diag-bmc.py set passthrough
    wedge100s-led-diag-bmc.py set off
    wedge100s-led-diag-bmc.py demo
"""

import json
import os
import sys
import time

RUN_DIR = "/run/wedge100s"
RESULTS_PATH = os.path.join(RUN_DIR, "led_diag_results.json")

# CPLD 0x3c preset values
PATTERNS = {
    "off":         0x00,
    "passthrough": 0x02,
    "walk":        0x08,
    "solid0":      0x80,
    "solid1":      0x90,
    "solid2":      0xA0,
    "solid3":      0xB0,
    "rainbow":     0xE0,
}

# 0x3c bit field decoders
def decode_led_ctrl(val):
    """Decode 0x3c register into human-readable fields."""
    return {
        "raw": "0x%02x" % val,
        "test_mode_en": bool(val & 0x80),
        "test_blink_en": bool(val & 0x40),
        "th_led_steam": (val >> 4) & 0x03,
        "walk_test_en": bool(val & 0x08),
        "th_led_en": bool(val & 0x02),
        "th_led_clear": bool(val & 0x01),
    }


def daemon_write_led_ctrl(value):
    """Write a value to CPLD 0x3c via bmc-daemon dispatch.

    Writes the desired value to /run/wedge100s/led_ctrl_write.set.
    The bmc-daemon picks this up via inotify, writes to CPLD, reads back,
    and stores the readback in /run/wedge100s/cpld_led_ctrl.
    """
    setfile = os.path.join(RUN_DIR, "led_ctrl_write.set")
    with open(setfile, "w") as f:
        f.write("0x%02x\n" % (value & 0xFF))


def daemon_read_led_ctrl():
    """Trigger a CPLD 0x3c read via bmc-daemon dispatch.

    Writes /run/wedge100s/cpld_led_ctrl.set to trigger a read.
    Returns None — caller must poll cpld_led_ctrl file for result.
    """
    setfile = os.path.join(RUN_DIR, "cpld_led_ctrl.set")
    with open(setfile, "w") as f:
        f.write("\n")


def daemon_read_led_color():
    """Trigger a CPLD 0x3d read via bmc-daemon dispatch."""
    setfile = os.path.join(RUN_DIR, "led_color_read.set")
    with open(setfile, "w") as f:
        f.write("\n")


def snapshot_mtime(name):
    """Return current mtime of /run/wedge100s/<name>, or 0 if missing."""
    path = os.path.join(RUN_DIR, name)
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def read_result_file(name, old_mtime=0, timeout=5.0):
    """Read an integer result from /run/wedge100s/<name>.

    Polls until the file mtime is strictly greater than old_mtime, or
    timeout. Returns int or None. Caller should capture old_mtime with
    snapshot_mtime() BEFORE triggering the daemon action.
    """
    path = os.path.join(RUN_DIR, name)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            mtime = os.path.getmtime(path)
            if mtime > old_mtime:
                with open(path) as f:
                    return int(f.read().strip())
        except (OSError, ValueError):
            pass
        time.sleep(0.1)

    # Timeout — try reading whatever's there
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_and_verify(value, label="", retries=3):
    """Write a CPLD 0x3c value via daemon, read back, compare.

    Retries the write if the inotify event was lost (coalesced by kernel
    when the daemon is busy with its 10-second sensor poll tick).
    Returns dict with intended, actual, match fields.
    """
    actual = None
    for attempt in range(1 + retries):
        mt = snapshot_mtime("cpld_led_ctrl")
        daemon_write_led_ctrl(value)
        actual = read_result_file("cpld_led_ctrl", mt, timeout=12.0)
        if actual == value:
            break
        if attempt < retries:
            # inotify event was likely coalesced — wait for daemon to
            # finish its current cycle before retrying
            time.sleep(2)

    result = {
        "label": label,
        "intended": "0x%02x" % value,
        "actual": "0x%02x" % actual if actual is not None else "TIMEOUT",
        "match": actual == value if actual is not None else False,
    }

    status = "PASS" if result["match"] else "FAIL"
    print("  %s: intended=0x%02x actual=%s  [%s]" % (
        label or "write", value,
        "0x%02x" % actual if actual is not None else "TIMEOUT",
        status))

    return result


def cmd_status():
    """Read and decode CPLD LED registers via bmc-daemon."""
    mt_ctrl = snapshot_mtime("cpld_led_ctrl")
    mt_color = snapshot_mtime("cpld_led_color")
    daemon_read_led_ctrl()
    daemon_read_led_color()

    ctrl = read_result_file("cpld_led_ctrl", mt_ctrl)
    color = read_result_file("cpld_led_color", mt_color)

    if ctrl is None:
        print("ERROR: could not read CPLD 0x3c via daemon (timeout)")
        print("Is wedge100s-bmc-daemon running? Check: systemctl status wedge100s-bmc-daemon")
        sys.exit(1)

    info = decode_led_ctrl(ctrl)
    print("=== CPLD LED Control (0x3c) via bmc-daemon ===")
    print("  raw value:      %s" % info["raw"])
    print("  test_mode_en:   %s" % info["test_mode_en"])
    print("  test_blink_en:  %s" % info["test_blink_en"])
    print("  th_led_steam:   %d" % info["th_led_steam"])
    print("  walk_test_en:   %s" % info["walk_test_en"])
    print("  th_led_en:      %s" % info["th_led_en"])
    print("  th_led_clear:   %s" % info["th_led_clear"])
    if color is not None:
        print("\n=== CPLD Test Color (0x3d) ===")
        print("  raw value:      0x%02x" % color)

    if info["th_led_en"] and not info["test_mode_en"]:
        print("\nMode: PASSTHROUGH (Tomahawk controls LEDs)")
    elif info["test_mode_en"] and info["test_blink_en"]:
        print("\nMode: RAINBOW (test mode + blink)")
    elif info["test_mode_en"]:
        print("\nMode: TEST SOLID (th_led_steam=%d)" % info["th_led_steam"])
    elif info["walk_test_en"]:
        print("\nMode: WALK TEST")
    elif ctrl == 0:
        print("\nMode: ALL OFF")
    else:
        print("\nMode: UNKNOWN (0x%02x)" % ctrl)


def cmd_set(mode, steam=None):
    """Set CPLD LED mode via bmc-daemon, verify readback."""
    if mode == "solid" and steam is not None:
        key = "solid%d" % steam
        label = "solid steam=%d" % steam
    else:
        key = mode
        label = mode

    if key not in PATTERNS:
        print("ERROR: unknown mode '%s'" % key)
        sys.exit(1)

    value = PATTERNS[key]
    print("Setting LED mode: %s (0x3c = 0x%02x)" % (label, value))
    result = write_and_verify(value, label)
    if not result["match"]:
        sys.exit(1)


def cmd_demo():
    """Automated demo: cycle through all patterns, verify each, save results."""
    sequence = [
        ("off", 0x00),
        ("solid steam=0", 0x80),
        ("solid steam=1", 0x90),
        ("solid steam=2", 0xA0),
        ("solid steam=3", 0xB0),
        ("rainbow", 0xE0),
        ("walk", 0x08),
        ("passthrough", 0x02),
    ]

    results = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "test": "led_cpld_demo",
        "steps": [],
    }

    all_pass = True
    for label, value in sequence:
        print("\n--- %s ---" % label)
        step = write_and_verify(value, label)
        results["steps"].append(step)
        if not step["match"]:
            all_pass = False
        if label != "passthrough":
            time.sleep(3)

    results["all_pass"] = all_pass
    print("\n=== Summary ===")
    print("Total: %d steps, %d passed, %d failed" % (
        len(results["steps"]),
        sum(1 for s in results["steps"] if s["match"]),
        sum(1 for s in results["steps"] if not s["match"]),
    ))

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print("Results saved to %s" % RESULTS_PATH)

    if not all_pass:
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        cmd_status()
    elif cmd == "set":
        if len(sys.argv) < 3:
            print("Usage: %s set <rainbow|solid|walk|passthrough|off>" % sys.argv[0])
            sys.exit(1)
        mode = sys.argv[2]
        steam = None
        if mode == "solid":
            if len(sys.argv) < 4:
                print("Usage: %s set solid <0-3>" % sys.argv[0])
                sys.exit(1)
            steam = int(sys.argv[3])
            if steam < 0 or steam > 3:
                print("ERROR: steam must be 0-3")
                sys.exit(1)
        cmd_set(mode, steam)
    elif cmd == "demo":
        cmd_demo()
    else:
        print("Unknown command: %s" % cmd)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
