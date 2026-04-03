"""Unit tests for wedge100s_ledup.py -- SOC parser, constants, port mapping.

Runs on dev host (no hardware required). Tests pure logic only.
"""
import os
import sys
import pytest

# The library lives in the platform utils directory
UTILS_DIR = os.path.join(
    os.path.dirname(__file__), "..",
    "platform", "broadcom", "sonic-platform-modules-accton",
    "wedge100s-32x", "utils",
)
sys.path.insert(0, UTILS_DIR)
import wedge100s_ledup as ledup

SOC_PATH = os.path.join(
    os.path.dirname(__file__), "..",
    "device", "accton", "x86_64-accton_wedge100s_32x-r0",
    "led_proc_init.soc",
)


class TestSocBytecodeParser:
    def test_parses_two_processors(self):
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        assert 0 in result and 1 in result

    def test_bytecode_length_le_256(self):
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        for proc, bytecodes in result.items():
            assert len(bytecodes) <= 256, f"LEDUP{proc} bytecode too long"

    def test_bytecodes_are_ints_0_to_255(self):
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        for proc, bytecodes in result.items():
            for b in bytecodes:
                assert 0 <= b <= 255

    def test_both_processors_have_identical_bytecode(self):
        """AS7712/Wedge100S: both processors run the same program."""
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        assert result[0] == result[1]

    def test_first_bytes_match_soc_file(self):
        """Verify against known first 4 bytes from led_proc_init.soc."""
        result = ledup.parse_soc_bytecodes(SOC_PATH)
        assert result[0][:4] == [0x02, 0xFD, 0x42, 0x80]


class TestSocRemapParser:
    def test_returns_32_port_mapping(self):
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert len(mapping) == 32

    def test_fp1_maps_to_data_ram_29(self):
        """FP1/Ethernet0 -> LED port 29 (from SOC file comments)."""
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert mapping[1] == 29

    def test_fp6_maps_to_data_ram_0(self):
        """FP6/Ethernet20 -> LED port 0."""
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert mapping[6] == 0

    def test_fp32_maps_to_data_ram_26(self):
        """FP32/Ethernet124 -> LED port 26."""
        mapping = ledup.parse_soc_remap(SOC_PATH)
        assert mapping[32] == 26

    def test_all_indices_unique(self):
        mapping = ledup.parse_soc_remap(SOC_PATH)
        values = list(mapping.values())
        assert len(values) == len(set(values)), "Duplicate DATA_RAM indices"

    def test_all_indices_in_range(self):
        mapping = ledup.parse_soc_remap(SOC_PATH)
        for fp, idx in mapping.items():
            assert 0 <= idx <= 31, f"FP{fp} maps to out-of-range index {idx}"


class TestConstants:
    def test_ledup0_offsets(self):
        assert ledup.LEDUP0_CTRL == 0x34000
        assert ledup.LEDUP0_PROGRAM_RAM_BASE == 0x34100
        assert ledup.LEDUP0_DATA_RAM_BASE == 0x34800

    def test_ledup1_offsets(self):
        assert ledup.LEDUP1_CTRL == 0x34400
        assert ledup.LEDUP1_PROGRAM_RAM_BASE == 0x34500
        assert ledup.LEDUP1_DATA_RAM_BASE == 0x34C00

    def test_program_ram_offset_helper(self):
        assert ledup.program_ram_offset(0, 0) == 0x34100
        assert ledup.program_ram_offset(0, 10) == 0x34100 + 40
        assert ledup.program_ram_offset(1, 0) == 0x34500

    def test_data_ram_offset_helper(self):
        assert ledup.data_ram_offset(0, 0) == 0x34800
        assert ledup.data_ram_offset(0, 29) == 0x34800 + 29 * 4
        assert ledup.data_ram_offset(1, 0) == 0x34C00
