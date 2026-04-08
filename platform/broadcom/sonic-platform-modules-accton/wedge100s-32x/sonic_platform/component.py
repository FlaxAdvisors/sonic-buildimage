"""Component — CPLD and BIOS firmware version reporting."""

import subprocess

CPLD_VERSION_PATH = "/run/wedge100s/cpld_version"


class Component:
    """Read-only firmware component (CPLD or BIOS).

    Wraps a version-reading callable so that chassis.py can expose both
    components through a uniform get_firmware_version() interface without
    knowing the details of how each version is retrieved.
    """

    def __init__(self, name, description, version_fn):
        """Initialize a firmware component.

        Args:
            name: Short component name (e.g. "CPLD", "BIOS").
            description: Human-readable description string.
            version_fn: Zero-argument callable that returns the version
                string or raises an exception on failure.
        """
        self._name = name
        self._description = description
        self._version_fn = version_fn

    def get_name(self):
        """Return the component name string."""
        return self._name

    def get_description(self):
        """Return the component description string."""
        return self._description

    def get_firmware_version(self):
        """Return the firmware version string, or 'N/A (<error>)' on failure.

        Calls the version_fn provided at construction.  Never raises.
        """
        try:
            return self._version_fn()
        except Exception as exc:
            return "N/A ({})".format(exc)

    def install_firmware(self, image_path):
        """Firmware installation not supported; returns False.

        Args:
            image_path: Path to firmware image (unused).

        Returns:
            bool: Always False — in-service firmware update not implemented.
        """
        return False

    def auto_update_firmware(self, image_path, boot_type):
        """Auto-update not supported; returns False.

        Args:
            image_path: Path to firmware image (unused).
            boot_type: Boot type hint (unused).

        Returns:
            bool: Always False — auto-update not implemented.
        """
        return False


def _cpld_version():
    """Read CPLD version string from the daemon cache file.

    Returns:
        str: Version string written by wedge100s-i2c-daemon at boot.

    Raises:
        OSError: If the daemon cache file does not exist yet.
    """
    return open(CPLD_VERSION_PATH).read().strip()


def _bios_version():
    """Read BIOS version string via dmidecode.

    Returns:
        str: BIOS version string from DMI table.

    Raises:
        RuntimeError: If dmidecode returns a non-zero exit code.
    """
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
