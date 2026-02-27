#!/usr/bin/env python
#
# Copyright (C) 2024 Accton Networks, Inc.
#
# Platform initialization utility for Accton Wedge 100S-32X.
#
# Architecture notes:
#   - Host I2C master is the CP2112 USB HID bridge (i2c-1, driver: hid_cp2112).
#     There is NO iSMT controller on this platform.
#   - Thermal sensors and fans are managed by OpenBMC via /dev/ttyACM0 (57600 8N1).
#     They are NOT accessible via host I2C.  Do NOT load lm75 or i2c_ismt.
#   - PSU presence/status is read from CPLD register 0x10 at i2c-1/0x32.
#   - QSFP28 presence is via PCA9535 GPIO expanders at i2c-36/0x22 and i2c-37/0x23.
#   - System EEPROM (ONIE TLV) was thought to be a 24c64 at i2c-40/0x50 - but then it moved?
#   - System EEPROM (ONIE TLV, TlvInfo) is an AT24C02 at i2c-1/0x51 (registered via i2c-40/0x51).
#   - Bus numbers are confirmed stable on SONiC kernel 6.1.0; see i2c_bus_map.json.

"""
Usage: %(scriptName)s [options] command object

options:
    -h | --help     : this help message
    -d | --debug    : run with debug mode
    -f | --force    : ignore errors during install or clean
command:
    install     : load drivers and register I2C devices
    clean       : unregister I2C devices and unload drivers
    show        : show platform status (PSU and QSFP presence)
    sff         : dump QSFP EEPROM for port 1-32
    set         : set fan <0-100> | led <0-4> | sfp <port> <0|1>
"""

import subprocess
import getopt
import sys
import logging
import os
import time

PROJECT_NAME = 'wedge100s_32x'
version = '0.1.0'
DEBUG = False
args = []
FORCE = 0

# EEPROM cache — written once at platform-init time, before xcvrd/pmon start.
# This prevents CP2112 I2C bus hangs caused by mux 0x74 contention between
# EEPROM reads (channel 6) and PCA9535 presence polls (channels 2 and 3).
EEPROM_SYSFS_PATH = '/sys/bus/i2c/devices/40-0051/eeprom'
EEPROM_CACHE_PATH = '/var/run/platform_cache/syseeprom_cache'
TLVINFO_MAGIC     = bytes([0x54, 0x6c, 0x76, 0x49, 0x6e, 0x66, 0x6f, 0x00])

# Port-to-I2C-bus map (ONL sfpi.c sfp_bus_index[], 0-indexed, confirmed SONiC 6.1.0)
SFP_BUS_MAP = [
     3,  2,  5,  4,  7,  6,  9,  8,
    11, 10, 13, 12, 15, 14, 17, 16,
    19, 18, 21, 20, 23, 22, 25, 24,
    27, 26, 29, 28, 31, 30, 33, 32,
]
NUM_SFP = 32

# PCA9535 QSFP presence: mux 0x74 ch2 = i2c-36, ch3 = i2c-37
_PRESENCE_BUS  = [36, 37]   # index 0 = ports 0-15, index 1 = ports 16-31
_PRESENCE_ADDR = [0x22, 0x23]

# CPLD location (PSU presence/status register 0x10)
_CPLD_BUS  = 1
_CPLD_ADDR = 0x32
_PSU_REG   = 0x10

# Kernel modules (load order matters; hid_cp2112 must precede i2c_mux_pca954x).
# i2c_ismt and lm75 are intentionally absent (no iSMT on this platform;
# thermal sensors are on BMC I2C, not host-accessible).
kos = [
    'modprobe i2c_dev',
    'modprobe i2c_i801',
    'modprobe hid_cp2112',
    'modprobe i2c_mux_pca954x force_deselect_on_exit=1',
    'modprobe at24',
    'modprobe optoe',
]

# I2C device registration.  Order is critical for bus number stability:
# PCA9548 muxes at 0x70–0x74 must be registered in address order so that
# the kernel assigns buses i2c-2 through i2c-41 matching i2c_bus_map.json.
# Child devices on mux 0x74 channels (i2c-36, i2c-37, i2c-40) come last.
mknod = [
    'echo pca9548 0x70 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x71 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x72 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x73 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x74 > /sys/bus/i2c/devices/i2c-1/new_device',
    # mux 0x74 ch2 → i2c-36: PCA9535 GPIO (QSFP presence ports 0-15)
    # mux 0x74 ch3 → i2c-37: PCA9535 GPIO (QSFP presence ports 16-31)
    # mux 0x74 ch6 → i2c-40: used to register system EEPROM at 0x51.
    #
    # COME module I2C topology (devices are directly on i2c-1, NOT behind mux):
    #   i2c-1/0x50: COME EC chip — 1-byte I2C register interface, ODM format.
    #               NOT writable via standard AT24 protocol (accepts write ACKs
    #               but data is not stored).  Do NOT register as at24.
    #   i2c-1/0x51: AT24C02 EEPROM — writable, holds ONIE TlvInfo for SONiC.
    #               Registered via bus 40 (transparent to i2c-1 for COME devices).
    #
    # The CP2112 cannot hold mux channel selection between HID transactions, so
    # all of mux 0x74's channels are non-isolating for COME module devices.
    'echo pca9535 0x22 > /sys/bus/i2c/devices/i2c-36/new_device',
    'echo pca9535 0x23 > /sys/bus/i2c/devices/i2c-37/new_device',
    'echo 24c02 0x51 > /sys/bus/i2c/devices/i2c-40/new_device',
]


def main():
    global DEBUG, args, FORCE

    if len(sys.argv) < 2:
        show_help()

    options, args = getopt.getopt(sys.argv[1:], 'hdf',
                                  ['help', 'debug', 'force'])
    for opt, arg in options:
        if opt in ('-h', '--help'):
            show_help()
        elif opt in ('-d', '--debug'):
            DEBUG = True
            logging.basicConfig(level=logging.INFO)
        elif opt in ('-f', '--force'):
            FORCE = 1

    for arg in args:
        if arg == 'install':
            do_install()
        elif arg == 'clean':
            do_uninstall()
        elif arg == 'show':
            device_traversal()
        elif arg == 'sff':
            if len(args) != 2:
                show_eeprom_help()
            elif not (1 <= int(args[1]) <= NUM_SFP):
                show_eeprom_help()
            else:
                show_eeprom(args[1])
            return
        elif arg == 'set':
            if len(args) < 2:
                show_set_help()
            else:
                set_device(args[1:])
            return
        else:
            show_help()

    return 0


def show_help():
    print(__doc__ % {'scriptName': sys.argv[0].split("/")[-1]})
    sys.exit(0)


def show_set_help():
    cmd = sys.argv[0].split("/")[-1] + " " + args[0]
    print(cmd + " [fan|led|sfp]")
    print("    use \"" + cmd + " fan 0-100\" to set fan duty % (requires BMC TTY — Phase 2)")
    print("    use \"" + cmd + " led 0-4\"   to set diag LED color (Phase 9)")
    print("    use \"" + cmd + " sfp 1-{} {{0|1}}\" to set QSFP tx_disable".format(NUM_SFP))
    sys.exit(0)


def show_eeprom_help():
    cmd = sys.argv[0].split("/")[-1] + " " + args[0]
    print("    use \"" + cmd + " 1-{}\" to dump QSFP EEPROM".format(NUM_SFP))
    sys.exit(0)


def my_log(txt):
    if DEBUG:
        print("[DBG] " + txt)


def log_os_system(cmd, show):
    logging.info('Run :' + cmd)
    status, output = subprocess.getstatusoutput(cmd)
    my_log(cmd + " => " + str(status))
    if output:
        my_log("  " + output)
    if status and show:
        print('Failed: ' + cmd)
    return status, output


def driver_check():
    """Return True if hid_cp2112 is loaded (CP2112 bridge / i2c-1 usable)."""
    ret, _ = log_os_system("lsmod | grep -q hid_cp2112", 0)
    return ret == 0


def device_exist():
    """Return True if the first PCA9548 mux (0x70) is registered on i2c-1."""
    ret, _ = log_os_system("ls /sys/bus/i2c/devices/1-0070", 0)
    return ret == 0


def system_ready():
    return driver_check() and device_exist()


def driver_install():
    global FORCE
    log_os_system("depmod", 1)
    for ko in kos:
        status, _ = log_os_system(ko, 1)
        if status and FORCE == 0:
            return status
    return 0


def driver_uninstall():
    global FORCE
    for ko in reversed(kos):
        rm = ko.replace("modprobe", "modprobe -rq")
        status, _ = log_os_system(rm, 1)
        if status and FORCE == 0:
            return status
    return 0


def device_install():
    global FORCE
    for cmd in mknod:
        if 'pca954' in cmd:
            time.sleep(0.5)  # allow kernel to enumerate new mux channels
        status, output = log_os_system(cmd, 1)
        if status:
            print(output)
            if FORCE == 0:
                return status
    return 0


def device_uninstall():
    """Unregister devices in reverse registration order."""
    global FORCE
    for cmd in reversed(mknod):
        parts = cmd.split()
        # parts: ['echo', '<driver>', '<addr>', '>', '/sys/.../new_device']
        addr = parts[2]
        target = parts[-1].replace('new_device', 'delete_device')
        rm_cmd = "echo {} > {}".format(addr, target)
        status, output = log_os_system(rm_cmd, 1)
        if status:
            print(output)
            if FORCE == 0:
                return status
    return 0


def _cache_eeprom():
    """Cache EEPROM raw bytes to a file immediately after device registration.

    This must be called before xcvrd/pmon start polling the PCA9535 presence
    expanders on mux 0x74 channels 2 and 3.  Those polls race with EEPROM reads
    on channel 6 of the same mux, causing the CP2112 I2C bus to hang (all reads
    return 0x00 bytes).  By caching here — while only the platform-init service
    is active — we guarantee that sonic_platform/eeprom.py returns valid TLV data
    for the lifetime of the boot, regardless of later bus state.

    Skips writing if the cache file already contains valid ONIE TlvInfo data.
    """
    if os.path.isfile(EEPROM_CACHE_PATH):
        try:
            with open(EEPROM_CACHE_PATH, 'rb') as f:
                cached_magic = f.read(8)
            if cached_magic == TLVINFO_MAGIC:
                my_log("EEPROM cache already valid: {}".format(EEPROM_CACHE_PATH))
                return
        except Exception:
            pass  # fall through to re-read hardware

    # Give the at24 driver a moment to finish probing the newly registered device
    time.sleep(0.2)

    try:
        with open(EEPROM_SYSFS_PATH, 'rb') as f:
            data = f.read()
    except Exception as e:
        print("WARNING: Could not read EEPROM from {}: {}".format(EEPROM_SYSFS_PATH, e))
        return

    if data[:8] != TLVINFO_MAGIC:
        print("WARNING: EEPROM does not contain ONIE TlvInfo magic "
              "(got: {}).  Cache NOT written.".format(data[:8].hex()))
        print("         Program the EEPROM with: sudo write-syseeprom "
              "-t 0x21 -v 'Wedge-100s-32X' -t 0x22 -v '<part>' "
              "-t 0x23 -v '<serial>' -t 0x24 -v '<mac>' -t 0x2b -v 'Accton'")
        return

    try:
        os.makedirs(os.path.dirname(EEPROM_CACHE_PATH), exist_ok=True)
        with open(EEPROM_CACHE_PATH, 'wb') as f:
            f.write(data)
        print("EEPROM cached to {} ({} bytes)".format(EEPROM_CACHE_PATH, len(data)))
    except Exception as e:
        print("WARNING: Could not write EEPROM cache {}: {}".format(EEPROM_CACHE_PATH, e))


def do_install():
    print("Checking system...")
    if not driver_check():
        print("Loading drivers...")
        status = driver_install()
        if status and FORCE == 0:
            return status
    else:
        print(PROJECT_NAME.upper() + " drivers already loaded.")
    if not device_exist():
        print("Registering I2C devices...")
        status = device_install()
        if status and FORCE == 0:
            return status
    else:
        print(PROJECT_NAME.upper() + " I2C devices already registered.")
    _cache_eeprom()
    print("Platform init complete.")


def do_uninstall():
    print("Checking system...")
    if device_exist():
        print("Unregistering I2C devices...")
        status = device_uninstall()
        if status and FORCE == 0:
            return status
    else:
        print(PROJECT_NAME.upper() + " has no devices to unregister.")
    if driver_check():
        print("Unloading drivers...")
        status = driver_uninstall()
        if status and FORCE == 0:
            return status
    else:
        print(PROJECT_NAME.upper() + " has no drivers to unload.")


# ── PCA9535 presence helpers ──────────────────────────────────────────────────

def _bit_swap(value):
    """Swap even/odd bit pairs per ONL sfpi.c onlp_sfpi_reg_val_to_port_sequence().
    Corrects PCA9535 GPIO wiring vs. front-panel QSFP port order."""
    result = 0
    for i in range(8):
        if i % 2 == 1:
            result |= (value & (1 << i)) >> 1
        else:
            result |= (value & (1 << i)) << 1
    return result


def _qsfp_present(port_num):
    """Return True if QSFP port_num (0-based) has a module inserted."""
    group  = 0 if port_num < 16 else 1
    bus    = _PRESENCE_BUS[group]
    addr   = _PRESENCE_ADDR[group]
    local  = port_num % 16
    offset = 0 if local < 8 else 1
    try:
        cmd = "i2cget -f -y {} 0x{:02x} 0x{:02x}".format(bus, addr, offset)
        out = subprocess.check_output(cmd, shell=True).decode().strip()
        swapped = _bit_swap(int(out, 0))
        return not bool(swapped & (1 << (port_num % 8)))
    except Exception:
        return False


# ── show / device_traversal ───────────────────────────────────────────────────

def device_traversal():
    if not system_ready():
        print("System not ready. Run 'install' first.")
        return

    print("=" * 50)
    print("PSU:")
    print("=" * 50)
    try:
        out = subprocess.check_output(
            "i2cget -f -y {} 0x{:02x} 0x{:02x}".format(
                _CPLD_BUS, _CPLD_ADDR, _PSU_REG),
            shell=True).decode().strip()
        val = int(out, 0)
        pres_bits  = [0, 4]
        pgood_bits = [1, 5]
        for idx in range(2):
            present = not bool(val & (1 << pres_bits[idx]))
            pgood   = bool(val & (1 << pgood_bits[idx]))
            state   = "present" if present else "absent"
            if present:
                state += ", power good" if pgood else ", power FAIL"
            print("  PSU{}: {}".format(idx + 1, state))
    except Exception as e:
        print("  CPLD read failed: {}".format(e))

    print()
    print("=" * 50)
    print("QSFP Presence (ports 1-32):")
    print("=" * 50)
    for port in range(NUM_SFP):
        present = _qsfp_present(port)
        print("  Port {:2d}: {}".format(port + 1, "present" if present else "absent"))

    print()
    print("Note: Fans and thermal sensors are managed by OpenBMC via /dev/ttyACM0.")
    print("      Use BMC TTY to query fan RPM and temperatures (Phase 2+).")


# ── sff (QSFP EEPROM dump) ────────────────────────────────────────────────────

def show_eeprom(index):
    if not system_ready():
        print("System not ready. Run 'install' first.")
        return

    port = int(index) - 1  # convert 1-based to 0-based
    bus = SFP_BUS_MAP[port]
    eeprom_path = "/sys/class/i2c-adapter/i2c-{0}/{0}-0050/eeprom".format(bus)

    if not subprocess.getstatusoutput("ls " + eeprom_path)[0] == 0:
        print("Port {} eeprom not found at {}.".format(index, eeprom_path))
        print("Ensure optoe1 is registered on i2c-{} (enabled in Phase 6).".format(bus))
        return

    ret, log = log_os_system("which hexdump", 0)
    if ret == 0:
        hex_cmd = 'hexdump'
    else:
        ret, log = log_os_system("which busybox", 0)
        if ret == 0:
            hex_cmd = 'busybox hexdump'
        else:
            print("hexdump not found.")
            return 1

    print("Port {} EEPROM (i2c-{}, {}):".format(index, bus, eeprom_path))
    ret, log = log_os_system("cat {} | {} -C".format(eeprom_path, hex_cmd), 1)
    if ret == 0:
        print(log)
    else:
        print("No module present or eeprom not readable.")


# ── set ───────────────────────────────────────────────────────────────────────

def set_device(args):
    if not system_ready():
        print("System not ready. Run 'install' first.")
        return

    if args[0] == 'fan':
        if len(args) < 2:
            show_set_help()
            return
        try:
            pct = int(args[1])
        except ValueError:
            show_set_help()
            return
        if not (0 <= pct <= 100):
            show_set_help()
            return
        # Fan speed is set via BMC TTY using set_fan_speed.sh (implemented in bmc.py / fan.py).
        # This CLI shim invokes it directly without the pmon sonic_platform layer.
        status, output = log_os_system(
            "i2cset -f -y 1 0x32 0x3e 0x02", 0)  # keep SYS1 green while adjusting
        status, output = log_os_system(
            "python3 -c \"import sys; sys.path.insert(0,'/usr/lib/python3/dist-packages');"
            "from sonic_platform import bmc; "
            "r=bmc.send_command('set_fan_speed.sh {}'); "
            "print('OK' if r else 'FAILED')\"".format(pct), 1)
        print("Fan speed set to {}%: {}".format(pct, output.strip()))
    elif args[0] == 'led':
        # SYS1 (0x3e) and SYS2 (0x3f) via CPLD at i2c-1/0x32.
        # Color map: 0=off, 1=red, 2=green, 4=blue.
        if len(args) < 2:
            show_set_help()
            return
        try:
            color = int(args[1])
        except ValueError:
            show_set_help()
            return
        log_os_system("i2cset -f -y 1 0x32 0x3e 0x{:02x}".format(color), 1)
        log_os_system("i2cset -f -y 1 0x32 0x3f 0x{:02x}".format(color), 1)
        print("LED color set to 0x{:02x}".format(color))
    elif args[0] == 'sfp':
        # QSFP LP_MODE / RESET pins are on the mux board and not accessible
        # from the host CPU on this platform.  tx_disable is not supported.
        print("QSFP tx_disable is not accessible from the host CPU on this platform.")
    else:
        show_set_help()


if __name__ == "__main__":
    main()
