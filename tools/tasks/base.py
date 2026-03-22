"""Base classes for ConfigTask pipeline."""
from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class Change:
    """Describes a single pending config change."""
    item: str     # human-readable description
    current: str  # observed current value
    desired: str  # target value
    cmd: str      # SONiC CLI command to apply this change

    def __repr__(self):
        return (
            f"Change({self.item!r}: {self.current!r} → {self.desired!r})"
        )


class ConfigTask(ABC):
    """Abstract base for all deploy tasks.

    Subclasses implement check(), apply(), and verify().
    deploy.py drives: check() → print changes → apply() → verify().
    """

    def __init__(self, ssh, topology: dict):
        self.ssh = ssh
        self.topology = topology

    @abstractmethod
    def check(self) -> list:
        """Query device state; return list[Change] for items that need updating.

        Must never modify state.
        """

    @abstractmethod
    def apply(self, changes: list) -> None:
        """Apply the list of changes returned by check()."""

    @abstractmethod
    def verify(self) -> bool:
        """Assert that post-apply state is correct. Return True on success."""
