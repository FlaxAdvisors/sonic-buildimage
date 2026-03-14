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
#   - System EEPROM (ONIE TLV, TlvInfo) is a 24c64 at 0x50 (registered via i2c-40/0x50).
#     ONIE registers it as: at24 7-0050 (8192 byte 24c64).  In SONiC, mux 0x74 ch6 = i2c-40.
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
import re
import time

PROJECT_NAME = 'wedge100s_32x'
PLATFORM_ROOT_PATH = '/usr/share/sonic/device'
PLATFORM_API2_WHL_FILE_PY3 = 'sonic_platform-1.0-py3-none-any.whl'
version = '0.1.0'
DEBUG = False
args = []
FORCE = 0

# System EEPROM sysfs path (at24 driver, registered by mknod above).
# The wedge100s-i2c-daemon reads this once at first boot and writes
# /run/wedge100s/syseeprom; eeprom.py reads the daemon cache as primary source.
EEPROM_SYSFS_PATH = '/sys/bus/i2c/devices/40-0050/eeprom'

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
    'modprobe wedge100s_cpld',
]

# I2C device registration.  Order is critical for bus number stability:
# PCA9548 muxes at 0x70–0x74 must be registered in address order so that
# the kernel assigns buses i2c-2 through i2c-41 matching i2c_bus_map.json.
# Child devices on mux 0x74 channels (i2c-36, i2c-37, i2c-40) come last.
# All 32 optoe1 QSFP EEPROM devices are pre-registered here (Phase R27) so
# that EEPROM sysfs paths exist before pmon/xcvrd start.  This fixes DAC
# cable EEPROM reads that fail with lazy per-port registration.
mknod = [
    # CPLD is directly on i2c-1 (not behind any mux); register first.
    'echo wedge100s_cpld 0x32 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x70 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x71 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x72 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x73 > /sys/bus/i2c/devices/i2c-1/new_device',
    'echo pca9548 0x74 > /sys/bus/i2c/devices/i2c-1/new_device',
    # mux 0x74 ch6 → i2c-40: system EEPROM (24c64 at 0x50, ONIE TlvInfo).
    # Note: PCA9535 at i2c-36/0x22 and i2c-37/0x23 are NOT registered as
    # gpio_pca953x kernel devices.  wedge100s-i2c-daemon reads them directly
    # via i2c-dev ioctl, eliminating the mux race from concurrent gpio sysfs
    # and EEPROM accesses on the same PCA9548 0x74 mux tree.
    'echo 24c64 0x50 > /sys/bus/i2c/devices/i2c-40/new_device',
] + [
    # optoe1 QSFP EEPROM on each port's dedicated bus (SFP_BUS_MAP order).
    # Registering all 32 here ensures /sys/bus/i2c/devices/i2c-N/N-0050/eeprom
    # exists before xcvrd starts, eliminating the lazy-init race.
    'echo optoe1 0x50 > /sys/bus/i2c/devices/i2c-{0}/new_device'.format(bus)
    for bus in SFP_BUS_MAP
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
    """Return True if all required modules are loaded (hid_cp2112 + i2c_dev)."""
    ret, _ = log_os_system("lsmod | grep -q hid_cp2112", 0)
    if ret != 0:
        return False
    ret, _ = log_os_system("lsmod | grep -q i2c_dev", 0)
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


def _pin_bcm_irq():
    """Manage IRQ affinity to prevent BCM56960 interrupt storms from stalling SSH.

    Root cause (confirmed 2026-03-12):
      - BCM56960 (linux-kernel-bde) is on XT-PIC IRQ 11, hardwired to CPU0.
        smp_affinity cannot move it.  Baseline: 150-700 IRQ/s.
      - 32 BGP neighbors configured on DOWN Ethernet* ports cause bgpd to retry
        ARP continuously.  The BCM ASIC CPU-traps these, spiking IRQ11 to
        5000-6000/s.  CPU0 softirq saturates, stalling NET_RX_SOFTIRQ.
      - eth0-TxRx-0 (PCI-MSI, typically IRQ 55) is also on CPU0 by default.
        During BCM bursts, eth0 RX processing halts, dropping all new TCP SYNs
        and ICMP — SSH blackouts of 30-50 s.

    Fix: move eth0-TxRx-0 to CPU2 (smp_affinity=4) so management-plane RX
    is isolated from BCM's interrupt load on CPU0.

    Dynamically discovers both IRQ numbers from /proc/interrupts so this
    remains correct across kernel versions.
    """
    try:
        with open('/proc/interrupts') as f:
            lines = f.readlines()
    except Exception as e:
        print("WARNING: Could not read /proc/interrupts: {}".format(e))
        return

    bcm_irq = None
    eth_irq = None
    for line in lines:
        if 'linux-kernel-bde' in line:
            bcm_irq = line.split(':')[0].strip()
        if re.search(r'\beth0-TxRx-0\b', line):
            eth_irq = line.split(':')[0].strip()

    if bcm_irq:
        my_log("BCM56960 (linux-kernel-bde) is on XT-PIC IRQ {} (CPU0 hardwired, "
               "cannot change affinity)".format(bcm_irq))
    else:
        my_log("WARNING: linux-kernel-bde not found in /proc/interrupts")

    if eth_irq:
        affinity_path = '/proc/irq/{}/smp_affinity'.format(eth_irq)
        try:
            with open(affinity_path, 'w') as f:
                f.write('4\n')   # CPU2 bitmask: isolate mgmt RX from CPU0 BCM storms
            my_log("eth0-TxRx-0 (IRQ {}) moved to CPU2 to isolate from BCM "
                   "interrupt storms on CPU0".format(eth_irq))
        except Exception as e:
            print("WARNING: Could not set eth0-TxRx-0 affinity (IRQ {}): {}".format(
                eth_irq, e))
    else:
        my_log("WARNING: eth0-TxRx-0 not found in /proc/interrupts — "
               "SSH may be affected by BCM interrupt storms")


def _warmup_qsfp_eeproms():
    """Prime DAC cable modules with a dummy read before xcvrd starts.

    Certain QSFP28 DAC cable modules (e.g. Joytech/Accton on bus 3, 7, 15)
    return identifier byte 0x01 (GBIC) on their very first i2c read after a
    cold/idle period, then correctly return 0x11 (QSFP28) on subsequent reads.
    xcvrd's startup scan hits each port exactly once; without priming it
    caches type=GBIC for these ports.

    This function reads 1 byte from each optoe1 eeprom sysfs file.  For
    present modules the read triggers the first i2c transaction, warming
    up the module's i2c interface.  xcvrd's subsequent startup read then
    gets the correct 0x11 identifier.
    """
    warmed = 0
    for bus in SFP_BUS_MAP:
        path = '/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom'.format(bus)
        try:
            with open(path, 'rb', buffering=0) as f:
                f.read(1)
            warmed += 1
        except OSError:
            pass   # absent port — normal
    print("QSFP EEPROM warm-up reads: {}/{} buses".format(warmed, len(SFP_BUS_MAP)))


def do_sonic_platform_install():
    device_path = "{}{}{}{}".format(PLATFORM_ROOT_PATH, '/x86_64-accton_', PROJECT_NAME, '-r0')
    SONIC_PLATFORM_BSP_WHL_PKG_PY3 = "/".join([device_path, PLATFORM_API2_WHL_FILE_PY3])

    status, output = log_os_system("pip3 show sonic-platform > /dev/null 2>&1", 0)
    if status:
        if os.path.exists(SONIC_PLATFORM_BSP_WHL_PKG_PY3):
            status, output = log_os_system("pip3 install " + SONIC_PLATFORM_BSP_WHL_PKG_PY3, 1)
            if status:
                print("Error: Failed to install {}".format(PLATFORM_API2_WHL_FILE_PY3))
                return status
            else:
                print("Successfully installed {} package".format(PLATFORM_API2_WHL_FILE_PY3))
        else:
            print('{} is not found'.format(SONIC_PLATFORM_BSP_WHL_PKG_PY3))
    else:
        print('{} has installed'.format(PLATFORM_API2_WHL_FILE_PY3))


def do_sonic_platform_clean():
    status, output = log_os_system("pip3 show sonic-platform > /dev/null 2>&1", 0)
    if status:
        print('{} does not install, not need to uninstall'.format(PLATFORM_API2_WHL_FILE_PY3))
    else:
        status, output = log_os_system("pip3 uninstall sonic-platform -y", 0)
        if status:
            print('Error: Failed to uninstall {}'.format(PLATFORM_API2_WHL_FILE_PY3))
        else:
            print('{} is uninstalled'.format(PLATFORM_API2_WHL_FILE_PY3))


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
    _warmup_qsfp_eeproms()
    _pin_bcm_irq()
    do_sonic_platform_install()
    print("Platform init complete.")


def do_uninstall():
    # Safety check: deleting PCA9548 mux devices while xcvrd holds the CP2112
    # I2C bus causes i2c_del_adapter() to block forever → kernel hung_task panic.
    # Refuse to uninstall if the pmon container is running.
    try:
        import subprocess as _sp
        status_out = _sp.run(
            ['docker', 'inspect', '--format={{.State.Status}}', 'pmon'],
            capture_output=True, text=True
        ).stdout.strip()
        if status_out == 'running':
            print("ABORT: pmon is running — stopping it would race with xcvrd on the I2C bus.")
            print("Run 'sudo systemctl stop pmon' and wait for xcvrd to exit before cleaning.")
            return 1
    except Exception:
        pass  # docker not available; proceed

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
    do_sonic_platform_clean()


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
    eeprom_path = "/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom".format(bus)

    if not os.path.exists(eeprom_path):
        print("Port {} eeprom not found at {}.".format(index, eeprom_path))
        print("Run 'install' to register I2C devices.")
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
