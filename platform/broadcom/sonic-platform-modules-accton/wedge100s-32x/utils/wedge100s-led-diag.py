#!/usr/bin/env python3
"""wedge100s-led-diag.py -- LED diagnostic and control tool for Wedge 100S-32X.

Requires root. All ASIC access via PCIe BAR2 /dev/mem (no bcmcmd dependency).
CPLD access via BMC SSH.

Usage:
    wedge100s-led-diag.py status
    wedge100s-led-diag.py set rainbow
    wedge100s-led-diag.py set passthrough
    wedge100s-led-diag.py set all-off
    wedge100s-led-diag.py set color <ledup0|ledup1|both|off>
    wedge100s-led-diag.py set port <1-32> <ledup0|ledup1|both|off>
    wedge100s-led-diag.py probe
"""

import argparse
import json
import os
import sys
import time

# Import shared library from same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wedge100s_ledup as ledup

SOC_PATH_DEVICE = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/led_proc_init.soc"
SOC_PATH_HWSKU = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0/Accton-WEDGE100S-32X/led_proc_init.soc"
PROBE_RESULTS_PATH = os.path.join(ledup.RUN_DIR, "led_probe_results.json")


def find_soc_path():
    """Find led_proc_init.soc on the target filesystem."""
    for p in (SOC_PATH_DEVICE, SOC_PATH_HWSKU):
        if os.path.exists(p):
            return p
    # Fallback: search device directory
    base = "/usr/share/sonic/device/x86_64-accton_wedge100s_32x-r0"
    for root, dirs, files in os.walk(base):
        if "led_proc_init.soc" in files:
            return os.path.join(root, "led_proc_init.soc")
    return None


def cmd_status(args):
    """Dump complete LED pipeline state."""
    cpld = ledup.CpldAccess()

    # CPLD state
    print("=== CPLD LED Control (0x3c) ===")
    try:
        val = cpld.read_led_ctrl()
        info = cpld.decode_led_ctrl(val)
        for k, v in info.items():
            print("  %-20s %s" % (k, v))
    except Exception as e:
        print("  ERROR reading CPLD: %s" % e)

    # LEDUP state
    with ledup.LedupAccess() as led:
        bar_addr, bar_size = led.bar2_info()
        print("\n=== BCM56960 BAR2: 0x%x (size 0x%x) ===" % (bar_addr, bar_size))

        for proc in (0, 1):
            ctrl = led.read_ctrl(proc)
            status = led.read_status(proc)
            print("\n--- LEDUP%d ---" % proc)
            print("  CTRL:   0x%08x  (LEDUP_EN=%d)" % (ctrl, ctrl & 1))
            print("  STATUS: 0x%08x" % status)

            # Check if any bytecode is loaded
            prog_nonzero = sum(
                1 for i in range(ledup.PROGRAM_RAM_SIZE)
                if led.read_program_ram(proc, i) != 0
            )
            print("  PROGRAM_RAM: %d/256 non-zero entries" % prog_nonzero)

            # DATA_RAM summary
            print("  DATA_RAM[0..31]:")
            for i in range(ledup.NUM_PORTS):
                val = led.read_data_ram(proc, i)
                if val != 0:
                    print("    [%2d] 0x%02x  %s" % (i, val, ledup.decode_data_ram(val)))
            nz = sum(1 for i in range(ledup.NUM_PORTS) if led.read_data_ram(proc, i) != 0)
            if nz == 0:
                print("    (all zero)")


def cmd_set_rainbow(args):
    """Set CPLD to test mode -- drives rainbow pattern from CPLD, no BCM."""
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_RAINBOW)
    readback = cpld.read_led_ctrl()
    if readback == ledup.CPLD_RAINBOW:
        print("PASS: CPLD 0x3c = 0x%02x (rainbow test mode)" % readback)
    else:
        print("FAIL: wrote 0x%02x, read back 0x%02x" % (ledup.CPLD_RAINBOW, readback))
        sys.exit(1)


def cmd_set_all_off(args):
    """Disable all port LEDs: CPLD off + LEDUP disabled + DATA_RAM zeroed."""
    # Disable CPLD passthrough and test modes
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_ALL_OFF)
    readback = cpld.read_led_ctrl()
    print("CPLD 0x3c: wrote 0x%02x, read 0x%02x -- %s" % (
        ledup.CPLD_ALL_OFF, readback,
        "PASS" if readback == ledup.CPLD_ALL_OFF else "FAIL"))

    # Disable LEDUP processors and zero DATA_RAM
    with ledup.LedupAccess() as led:
        for proc in (0, 1):
            led.write_ctrl(proc, 0x00000000)
            led.zero_data_ram(proc)
            ctrl = led.read_ctrl(proc)
            print("LEDUP%d CTRL: 0x%08x -- %s" % (
                proc, ctrl, "PASS" if ctrl == 0 else "FAIL"))

    print("All port LEDs disabled.")


def load_and_enable_ledup(led, soc_path):
    """Load bytecode from SOC file into PROGRAM_RAM, enable LEDUP processors.

    Args:
        led: LedupAccess instance (already open)
        soc_path: path to led_proc_init.soc

    Returns True on success, False on failure.
    """
    bytecodes = ledup.parse_soc_bytecodes(soc_path)
    if not bytecodes:
        print("ERROR: no bytecode found in %s" % soc_path)
        return False

    ok = True
    for proc in sorted(bytecodes.keys()):
        bc = bytecodes[proc]
        print("Loading LEDUP%d: %d bytes..." % (proc, len(bc)), end=" ")
        led.load_bytecode(proc, bc)
        verified, mismatch = led.verify_bytecode(proc, bc)
        if verified:
            print("verified OK")
        else:
            print("FAIL at index %d (wrote 0x%02x, read 0x%02x)" % (
                mismatch, bc[mismatch], led.read_program_ram(proc, mismatch)))
            ok = False

    if not ok:
        return False

    # Enable LEDUP processors.
    # CTRL register bit layout (from BCM56960 investigation):
    #   Bit 0: LEDUP_EN
    #   The exact bit positions for SCAN_START_DELAY and INTRA_PORT_DELAY
    #   are discovered empirically. Start with just LEDUP_EN=1 (value 0x01).
    #   If that doesn't work, try the full value from the investigation.
    #
    # Strategy: write CTRL=1, check STATUS for RUNNING bit. If not running
    # after a brief delay, try larger CTRL values with timing fields.
    ctrl_candidates = [
        0x00000001,          # minimal: just LEDUP_EN
        0x00002A41,          # LEDUP_EN + SCAN_START_DELAY=0x2a<<8 + INTRA_PORT_DELAY=4<<4
        0x0004_2A01,         # LEDUP_EN + INTRA_PORT_DELAY=4<<16 + SCAN_START_DELAY=0x2a<<8
    ]

    for proc in sorted(bytecodes.keys()):
        enabled = False
        for ctrl_val in ctrl_candidates:
            led.write_ctrl(proc, ctrl_val)
            time.sleep(0.05)
            status = led.read_status(proc)
            readback = led.read_ctrl(proc)
            if readback & 1:  # LEDUP_EN bit is set
                print("LEDUP%d: CTRL=0x%08x STATUS=0x%08x — enabled" % (
                    proc, readback, status))
                enabled = True
                break
            print("LEDUP%d: CTRL=0x%08x (tried 0x%08x, readback 0x%08x)" % (
                proc, readback, ctrl_val, readback))

        if not enabled:
            print("WARNING: LEDUP%d could not be enabled — check CTRL register format" % proc)
            ok = False

    return ok


def _ensure_bytecode_loaded(led):
    """Load bytecode if PROGRAM_RAM is empty. Returns True on success."""
    if led.read_program_ram(0, 0) != 0:
        return True
    soc_path = find_soc_path()
    if not soc_path:
        print("ERROR: led_proc_init.soc not found")
        return False
    return load_and_enable_ledup(led, soc_path)


def cmd_set_color(args):
    """Software-drive all 32 ports to a single color.

    Color is specified as ledup0/ledup1/both/off, referring to which
    LEDUP processor(s) output active (1) for all ports.
    """
    color = args.color
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)

    with ledup.LedupAccess() as led:
        if not _ensure_bytecode_loaded(led):
            sys.exit(1)

        for proc in (0, 1):
            if color == "off":
                led.zero_data_ram(proc)
                led.write_ctrl(proc, 0)
            else:
                active = (color == "both" or
                          (color == "ledup0" and proc == 0) or
                          (color == "ledup1" and proc == 1))
                if active:
                    for i in range(ledup.NUM_PORTS):
                        led.write_data_ram(proc, i, ledup.BIT_LINK)
                else:
                    led.zero_data_ram(proc)

        # Read back and report
        for proc in (0, 1):
            ctrl = led.read_ctrl(proc)
            nz = sum(1 for i in range(ledup.NUM_PORTS) if led.read_data_ram(proc, i) != 0)
            print("LEDUP%d: CTRL=0x%08x, DATA_RAM non-zero=%d/32" % (proc, ctrl, nz))

    print("Set all ports to: %s" % color)


def cmd_set_port(args):
    """Software-drive a single front-panel port to a color.

    All other ports are set to off. Uses the SOC file remap table
    to map front-panel port number to DATA_RAM index.
    """
    fp_port = args.port_num
    color = args.color
    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)

    soc_path = find_soc_path()
    if not soc_path:
        print("ERROR: led_proc_init.soc not found")
        sys.exit(1)

    remap = ledup.parse_soc_remap(soc_path)
    led_index = remap[fp_port]

    with ledup.LedupAccess() as led:
        if not _ensure_bytecode_loaded(led):
            sys.exit(1)

        for proc in (0, 1):
            led.zero_data_ram(proc)

            active = (color == "both" or
                      (color == "ledup0" and proc == 0) or
                      (color == "ledup1" and proc == 1))
            if active:
                led.write_data_ram(proc, led_index, ledup.BIT_LINK)

        for proc in (0, 1):
            val = led.read_data_ram(proc, led_index)
            print("LEDUP%d DATA_RAM[%d] = 0x%02x" % (proc, led_index, val))

    print("Set FP port %d (DATA_RAM[%d]) to: %s" % (fp_port, led_index, color))


def cmd_probe(args):
    """Discover LED color mapping through automated test sequences.

    Phase 1: CPLD test mode -- cycles through CPLD-generated patterns
    Phase 2: BCM scan chain -- tests all 4 LEDUP0/LEDUP1 combinations
    Phase 3: Per-port walk -- lights one port at a time

    Each phase pauses for manual observation. Results saved to JSON.
    """
    results = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "phases": {}}
    cpld = ledup.CpldAccess()

    # -- Phase 1: CPLD test mode colors
    print("=== Phase 1: CPLD Test Mode Colors ===")
    print("Observe the front panel LEDs and note the color/pattern.")
    phase1 = []

    for steam in range(4):
        val = ledup.CPLD_TEST_MODE_EN | (steam << 4)
        cpld.write_led_ctrl(val)
        readback = cpld.read_led_ctrl()
        desc = "th_led_steam=%d, 0x3c=0x%02x" % (steam, readback)
        print("\n[Phase 1.%d] %s" % (steam, desc))
        print("  Press Enter after observing (type color description): ", end="")
        observation = input().strip() or "not recorded"
        phase1.append({"steam": steam, "reg_0x3c": "0x%02x" % readback,
                       "observation": observation})

    for name, val in [("blink", 0xE0), ("walk", 0x08)]:
        cpld.write_led_ctrl(val)
        readback = cpld.read_led_ctrl()
        print("\n[Phase 1.%s] 0x3c=0x%02x" % (name, readback))
        print("  Press Enter after observing (type description): ", end="")
        observation = input().strip() or "not recorded"
        phase1.append({"mode": name, "reg_0x3c": "0x%02x" % readback,
                       "observation": observation})

    results["phases"]["cpld_test_modes"] = phase1

    # -- Phase 2: BCM scan chain combinations
    print("\n=== Phase 2: BCM Scan Chain Combinations ===")
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
    phase2 = []

    soc_path = find_soc_path()
    with ledup.LedupAccess() as led:
        if soc_path:
            _ensure_bytecode_loaded(led)

        combos = [
            ("off/off", False, False),
            ("on/off", True, False),
            ("off/on", False, True),
            ("on/on", True, True),
        ]

        for label, ledup0_on, ledup1_on in combos:
            for proc in (0, 1):
                active = (proc == 0 and ledup0_on) or (proc == 1 and ledup1_on)
                if active:
                    for i in range(ledup.NUM_PORTS):
                        led.write_data_ram(proc, i, ledup.BIT_LINK)
                else:
                    led.zero_data_ram(proc)

            print("\n[Phase 2] LEDUP0=%s LEDUP1=%s" % (
                "active" if ledup0_on else "off",
                "active" if ledup1_on else "off"))
            print("  Press Enter after observing (type color): ", end="")
            observation = input().strip() or "not recorded"
            phase2.append({
                "ledup0": "active" if ledup0_on else "off",
                "ledup1": "active" if ledup1_on else "off",
                "observation": observation,
            })

    results["phases"]["bcm_scan_chain"] = phase2

    # -- Phase 3: Per-port walk
    print("\n=== Phase 3: Per-Port Walk ===")
    print("Lighting one port at a time (both LEDUP channels).")
    phase3 = []

    if soc_path:
        remap = ledup.parse_soc_remap(soc_path)
    else:
        print("WARNING: no SOC file, using identity mapping")
        remap = {fp: fp - 1 for fp in range(1, 33)}

    with ledup.LedupAccess() as led:
        _ensure_bytecode_loaded(led)

        for fp in range(1, ledup.NUM_PORTS + 1):
            led_idx = remap[fp]
            for proc in (0, 1):
                led.zero_data_ram(proc)
                led.write_data_ram(proc, led_idx, ledup.BIT_LINK)

            print("[Phase 3] FP%d (DATA_RAM[%d]) -- observe which cage lights up: " % (
                fp, led_idx), end="")
            observation = input().strip() or "ok"
            phase3.append({"fp_port": fp, "data_ram_index": led_idx,
                           "observation": observation})

        for proc in (0, 1):
            led.zero_data_ram(proc)

    results["phases"]["per_port_walk"] = phase3

    # -- Save results
    os.makedirs(ledup.RUN_DIR, exist_ok=True)
    with open(PROBE_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to %s" % PROBE_RESULTS_PATH)

    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
    print("Restored CPLD to passthrough mode.")


def cmd_set_passthrough(args):
    """Restore full LED pipeline: CPLD passthrough + bytecode + auto.

    Equivalent to what ledinit should do via bcmcmd:
    1. Load led_proc_init.soc bytecode into PROGRAM_RAM
    2. Enable LEDUP processors
    3. Set CPLD to passthrough mode (0x02)

    PORT_ORDER_REMAP registers are NOT written (BAR2 offsets unknown).
    If port-to-LED mapping is wrong, remap offsets must be discovered
    separately via bcmcmd or SDK headers.
    """
    soc_path = find_soc_path()
    if not soc_path:
        print("ERROR: led_proc_init.soc not found")
        sys.exit(1)

    cpld = ledup.CpldAccess()
    cpld.write_led_ctrl(ledup.CPLD_PASSTHROUGH)
    readback = cpld.read_led_ctrl()
    print("CPLD 0x3c = 0x%02x -- %s" % (
        readback, "PASS" if readback == ledup.CPLD_PASSTHROUGH else "FAIL"))

    with ledup.LedupAccess() as led:
        ok = load_and_enable_ledup(led, soc_path)
        if ok:
            print("\nPassthrough mode active. LEDUP processors running.")
            print("Port LEDs should now reflect live link/speed status.")
            print("\nNOTE: PORT_ORDER_REMAP not configured via /dev/mem.")
            print("If port LEDs show wrong positions, remap offsets need discovery.")
        else:
            print("\nWARNING: Bytecode loaded but LEDUP enable may have failed.")
            print("Run 'status' to inspect register state.")

    print("\n--- Post-passthrough status ---")
    cmd_status(argparse.Namespace())


def main():
    parser = argparse.ArgumentParser(
        description="Wedge 100S-32X LED diagnostic and control tool",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Dump CPLD + LEDUP state")

    set_parser = sub.add_parser("set", help="Set LED mode")
    set_sub = set_parser.add_subparsers(dest="mode")
    set_sub.add_parser("rainbow", help="CPLD test mode (rainbow)")
    set_sub.add_parser("passthrough", help="CPLD passthrough + load bytecode")
    set_sub.add_parser("all-off", help="Disable all LEDs")
    color_parser = set_sub.add_parser("color", help="Software-drive all ports")
    color_parser.add_argument("color", choices=["ledup0", "ledup1", "both", "off"])
    port_parser = set_sub.add_parser("port", help="Software-drive single port")
    port_parser.add_argument("port_num", type=int, choices=range(1, 33), metavar="1-32")
    port_parser.add_argument("color", choices=["ledup0", "ledup1", "both", "off"])

    sub.add_parser("probe", help="Discover LED color mapping")

    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: must run as root", file=sys.stderr)
        sys.exit(1)

    if args.command == "status":
        cmd_status(args)
    elif args.command == "set":
        if args.mode == "rainbow":
            cmd_set_rainbow(args)
        elif args.mode == "passthrough":
            cmd_set_passthrough(args)
        elif args.mode == "all-off":
            cmd_set_all_off(args)
        elif args.mode == "color":
            cmd_set_color(args)
        elif args.mode == "port":
            cmd_set_port(args)
        else:
            set_parser.print_help()
    elif args.command == "probe":
        cmd_probe(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
