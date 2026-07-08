"""变量运行态监控单元测试。"""

import time
import struct

from cnrtt.watch import (
    MemoryWatchManager,
    _load_elf_symtab_symbols_raw,
    _location_to_address,
    format_watch_value,
    load_axf_symbols,
    value_type_size,
)


def test_format_watch_value_integer_and_float():
    assert format_watch_value(bytes([0x78, 0x56, 0x34, 0x12]), "u32") == "305419896 (0x12345678)"
    assert format_watch_value(bytes([0xFF]), "s8") == "-1 (0xFF)"
    assert format_watch_value(bytes([0x00, 0x00, 0x80, 0x3F]), "float") == "1"


def test_value_type_size():
    assert value_type_size("u8") == 1
    assert value_type_size("uint32_t") == 4
    assert value_type_size("double") == 8


def test_location_list_offset_is_ignored():
    assert _location_to_address(0x20000000, 4) is None


def test_memory_watch_manager_polls_due_items():
    updates = []

    def read_memory(address, size):
        assert address == 0x20000000
        assert size == 4
        return bytes([1, 0, 0, 0])

    mgr = MemoryWatchManager(read_memory=read_memory, on_update=updates.append)
    item = mgr.add_item("counter", 0x20000000, "u32", period_ms=50)
    try:
        mgr.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if updates and updates[-1][0].get("value"):
                break
            time.sleep(0.02)
    finally:
        mgr.stop()

    assert item["name"] == "counter"
    assert updates[-1][0]["value"] == "1 (0x00000001)"


def test_load_axf_symbols_from_minimal_elf_symtab(tmp_path):
    path = tmp_path / "test.axf"
    data = bytearray(0x340)
    data[0:16] = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)

    shoff = 0x100
    header = struct.pack(
        "<HHIIIIIHHHHHH",
        2,   # executable
        40,  # ARM
        1,
        0,
        0,
        shoff,
        0,
        52,
        0,
        0,
        40,
        4,
        0,
    )
    data[16 : 16 + len(header)] = header

    symtab_offset = 0x1C0
    strtab_offset = 0x240
    data_offset = 0x300
    strings = b"\0counter\0common_counter\0"
    data[strtab_offset : strtab_offset + len(strings)] = strings

    symtab = bytearray(48)
    symtab[16:32] = struct.pack(
        "<IIIBBH",
        1,  # name offset
        0x20000000,
        4,
        0x11,  # global object
        0,
        1,
    )
    symtab[32:48] = struct.pack(
        "<IIIBBH",
        9,  # name offset
        4,  # SHN_COMMON uses st_value as alignment, not runtime address
        4,
        0x11,  # global object
        0,
        0xFFF2,  # SHN_COMMON
    )
    data[symtab_offset : symtab_offset + len(symtab)] = symtab

    section_headers = [
        bytes(40),
        struct.pack("<IIIIIIIIII", 0, 1, 0x3, 0x20000000, data_offset, 4, 0, 0, 4, 0),
        struct.pack("<IIIIIIIIII", 0, 2, 0, 0, symtab_offset, len(symtab), 3, 0, 4, 16),
        struct.pack("<IIIIIIIIII", 0, 3, 0, 0, strtab_offset, len(strings), 0, 0, 1, 0),
    ]
    cursor = shoff
    for section in section_headers:
        data[cursor : cursor + len(section)] = section
        cursor += len(section)

    path.write_bytes(data)

    symbols = load_axf_symbols(str(path))
    assert _load_elf_symtab_symbols_raw(str(path)) == symbols
    assert symbols == [
        {
            "name": "counter",
            "address": 0x20000000,
            "address_hex": "0x20000000",
            "type": "u32",
            "size": 4,
        }
    ]
