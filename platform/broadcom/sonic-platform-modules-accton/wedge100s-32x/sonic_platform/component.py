"""Component — CPLD and BIOS firmware version reporting."""

import subprocess

CPLD_VERSION_PATH = "/sys/bus/i2c/devices/1-0032/cpld_version"


class Component:
    """Read-only firmware component (CPLD or BIOS)."""

    def __init__(self, name, description, version_fn):
        self._name = name
        self._description = description
        self._version_fn = version_fn

    def get_name(self):
        return self._name

    def get_description(self):
        return self._description

    def get_firmware_version(self):
        try:
            return self._version_fn()
        except Exception as exc:
            return "N/A ({})".format(exc)

    def install_firmware(self, image_path):
        return False

    def auto_update_firmware(self, image_path, boot_type):
        return False


def _cpld_version():
    return open(CPLD_VERSION_PATH).read().strip()


def _bios_version():
    result = subprocess.run(
        ["sudo", "dmidecode", "-s", "bios-version"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        return result.stdout.strip()
    raise RuntimeError(result.stderr.strip())


COMPONENT_CPLD = Component(
    name="CPLD",
    description="Complex Programmable Logic Device",
    version_fn=_cpld_version,
)

COMPONENT_BIOS = Component(
    name="BIOS",
    description="Basic Input/Output System",
    version_fn=_bios_version,
)

COMPONENT_LIST = [COMPONENT_CPLD, COMPONENT_BIOS]
