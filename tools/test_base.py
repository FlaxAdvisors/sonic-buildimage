"""Unit tests for ConfigTask base classes."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.tasks.base import Change, ConfigTask

def test_change_dataclass():
    c = Change(
        item="VRF mgmt",
        current="missing",
        desired="present",
        cmd="config vrf add mgmt",
    )
    assert c.item == "VRF mgmt"
    assert c.current == "missing"
    assert c.desired == "present"
    assert c.cmd == "config vrf add mgmt"

def test_change_repr_contains_item():
    c = Change(item="foo", current="a", desired="b", cmd="cmd")
    assert "foo" in repr(c)

class ConcreteTask(ConfigTask):
    def check(self):
        return []
    def apply(self, changes):
        pass
    def verify(self):
        return True

def test_concrete_task_instantiation():
    task = ConcreteTask(ssh=None, topology={})
    assert task.check() == []
    assert task.verify() is True

from tools.tasks.breakout import _expected_subports

def test_expected_subports_expansion():
    bps = [
        {"parent": "Ethernet0",  "mode": "4x25G[10G]"},
        {"parent": "Ethernet64", "mode": "4x25G[10G]"},
    ]
    result = _expected_subports(bps)
    assert result == [
        "Ethernet0", "Ethernet1", "Ethernet2", "Ethernet3",
        "Ethernet64", "Ethernet65", "Ethernet66", "Ethernet67",
    ]
