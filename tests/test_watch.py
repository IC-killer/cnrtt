"""变量运行态监控单元测试。"""

import time
import struct

import pytest

from cnrtt.watch import (
    MemoryWatchManager,
    WatchError,
    _add_dwarf_watch_symbols,
    _load_elf_symtab_symbols_raw,
    _location_to_address,
    _location_attr_to_address,
    format_watch_value,
    load_axf_symbols,
    value_type_size,
)


class _FakeAttr:
    def __init__(self, value, form=""):
        self.value = value
        self.form = form


class _FakeDie:
    def __init__(self, tag, attrs=None, offset=0, children=None):
        self.tag = tag
        self.attributes = attrs or {}
        self.offset = offset
        self._children = children or []

    def iter_children(self):
        return iter(self._children)


class _FakeCU:
    cu_offset = 0


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


def test_location_attr_reads_direct_location_list_expr():
    class Entry:
        loc_expr = bytes([0x03, 0x78, 0x56, 0x34, 0x12])

    class LocationLists:
        def get_location_list_at_offset(self, offset, die=None):
            assert offset == 0x40
            assert die is not None
            return [Entry()]

    class Dwarf:
        def location_lists(self):
            return LocationLists()

    assert (
        _location_attr_to_address(
            Dwarf(),
            _FakeDie("DW_TAG_variable"),
            _FakeAttr(0x40, form="DW_FORM_sec_offset"),
            4,
        )
        == 0x12345678
    )


def test_dwarf_symbol_expands_struct_array_enum_and_pointer():
    u32_die = _FakeDie(
        "DW_TAG_base_type",
        {
            "DW_AT_byte_size": _FakeAttr(4),
            "DW_AT_encoding": _FakeAttr(7),
        },
        offset=1,
    )
    u16_die = _FakeDie(
        "DW_TAG_base_type",
        {
            "DW_AT_byte_size": _FakeAttr(2),
            "DW_AT_encoding": _FakeAttr(7),
        },
        offset=2,
    )
    enum_die = _FakeDie(
        "DW_TAG_enumeration_type",
        {
            "DW_AT_name": _FakeAttr(b"Mode"),
            "DW_AT_byte_size": _FakeAttr(4),
        },
        offset=3,
        children=[
            _FakeDie(
                "DW_TAG_enumerator",
                {
                    "DW_AT_name": _FakeAttr(b"IDLE"),
                    "DW_AT_const_value": _FakeAttr(0),
                },
            ),
            _FakeDie(
                "DW_TAG_enumerator",
                {
                    "DW_AT_name": _FakeAttr(b"RUN"),
                    "DW_AT_const_value": _FakeAttr(1),
                },
            ),
        ],
    )
    array_die = _FakeDie(
        "DW_TAG_array_type",
        {"DW_AT_type": _FakeAttr(2)},
        offset=4,
        children=[
            _FakeDie("DW_TAG_subrange_type", {"DW_AT_count": _FakeAttr(3)}),
        ],
    )
    pointer_die = _FakeDie(
        "DW_TAG_pointer_type",
        {
            "DW_AT_byte_size": _FakeAttr(4),
            "DW_AT_type": _FakeAttr(6),
        },
        offset=5,
    )
    struct_die = _FakeDie(
        "DW_TAG_structure_type",
        {
            "DW_AT_name": _FakeAttr(b"State"),
            "DW_AT_byte_size": _FakeAttr(20),
        },
        offset=6,
        children=[
            _FakeDie(
                "DW_TAG_member",
                {
                    "DW_AT_name": _FakeAttr(b"counter"),
                    "DW_AT_type": _FakeAttr(1),
                    "DW_AT_data_member_location": _FakeAttr(0),
                },
            ),
            _FakeDie(
                "DW_TAG_member",
                {
                    "DW_AT_name": _FakeAttr(b"samples"),
                    "DW_AT_type": _FakeAttr(4),
                    "DW_AT_data_member_location": _FakeAttr(4),
                },
            ),
            _FakeDie(
                "DW_TAG_member",
                {
                    "DW_AT_name": _FakeAttr(b"mode"),
                    "DW_AT_type": _FakeAttr(3),
                    "DW_AT_data_member_location": _FakeAttr(10),
                },
            ),
            _FakeDie(
                "DW_TAG_member",
                {
                    "DW_AT_name": _FakeAttr(b"next"),
                    "DW_AT_type": _FakeAttr(5),
                    "DW_AT_data_member_location": _FakeAttr(16),
                },
            ),
        ],
    )
    die_by_offset = {
        1: u32_die,
        2: u16_die,
        3: enum_die,
        4: array_die,
        5: pointer_die,
        6: struct_die,
    }
    symbols = {}

    added = _add_dwarf_watch_symbols(
        cu=_FakeCU(),
        die_by_offset=die_by_offset,
        symbols=symbols,
        name="state",
        address=0x20000000,
        type_die=struct_die,
        address_size=4,
    )

    assert added == 6
    assert symbols["state.counter"].to_dict() == {
        "name": "state.counter",
        "address": 0x20000000,
        "address_hex": "0x20000000",
        "type": "u32",
        "size": 4,
        "parent": "state",
        "path": "state.counter",
    }
    assert symbols["state.samples[0]"].address == 0x20000004
    assert symbols["state.samples[0]"].value_type == "u16"
    assert symbols["state.samples[1]"].address == 0x20000006
    assert symbols["state.samples[2]"].address == 0x20000008
    assert symbols["state.mode"].to_dict()["detail"] == {
        "enum": {"IDLE": 0, "RUN": 1},
        "type_name": "Mode",
    }
    assert symbols["state.mode"].to_dict()["kind"] == "enum"
    assert symbols["state.next"].address == 0x20000010
    assert symbols["state.next"].value_type == "u32"
    assert symbols["state.next"].to_dict()["detail"] == {"target": "State"}


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
    assert updates[-1][0]["raw"] == "01 00 00 00"
    assert updates[-1][0]["read_count"] >= 1
    assert updates[-1][0]["fail_count"] == 0
    assert updates[-1][0]["latency_ms"] >= 0


def test_memory_watch_manager_records_failures_without_losing_last_value():
    updates = []
    calls = 0

    def read_memory(address, size):
        nonlocal calls
        calls += 1
        if calls == 1:
            return bytes([2, 0, 0, 0])
        raise RuntimeError("bus fault")

    mgr = MemoryWatchManager(read_memory=read_memory, on_update=updates.append)
    mgr.add_item("counter", 0x20000000, "u32", period_ms=50)
    try:
        mgr.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if updates and updates[-1][0].get("fail_count", 0) >= 1:
                break
            time.sleep(0.02)
    finally:
        mgr.stop()

    latest = updates[-1][0]
    assert latest["value"] == "2 (0x00000002)"
    assert latest["raw"] == "02 00 00 00"
    assert latest["error"] == "bus fault"
    assert latest["read_count"] >= 2
    assert latest["fail_count"] >= 1


def _due_items(manager):
    with manager._lock:
        return list(manager._items.values())


def test_memory_watch_manager_coalesces_adjacent_reads():
    calls = []
    base = 0x20000000
    memory = bytes(range(16))

    def read_memory(address, size):
        calls.append((address, size))
        offset = address - base
        return memory[offset : offset + size]

    mgr = MemoryWatchManager(
        read_memory=read_memory,
        on_update=lambda items: None,
        merge_gap=0,
    )
    mgr.add_item("counter", base, "u32", period_ms=50)
    mgr.add_item("sample", base + 4, "u16", period_ms=50)

    assert mgr._sample_due_items(_due_items(mgr)) is True

    assert calls == [(base, 6)]
    items = {item["name"]: item for item in mgr.list_items()}
    assert items["counter"]["raw"] == "00 01 02 03"
    assert items["sample"]["raw"] == "04 05"
    stats = mgr.get_stats()
    assert stats["due_count"] == 2
    assert stats["planned_read_calls"] == 1
    assert stats["read_calls"] == 1
    assert stats["sampled_count"] == 2
    assert stats["merge_saved_calls"] == 1
    assert stats["merge_ratio"] == 50.0


def test_memory_watch_manager_applies_read_call_budget():
    calls = []
    base = 0x20000000

    def read_memory(address, size):
        calls.append((address, size))
        return bytes([len(calls), 0, 0, 0])

    mgr = MemoryWatchManager(
        read_memory=read_memory,
        on_update=lambda items: None,
        max_read_calls_per_cycle=1,
        merge_gap=0,
    )
    mgr.add_item("a", base, "u32", period_ms=50)
    mgr.add_item("b", base + 0x100, "u32", period_ms=50)
    mgr.add_item("c", base + 0x200, "u32", period_ms=50)

    assert mgr._sample_due_items(_due_items(mgr)) is True

    assert calls == [(base, 4)]
    stats = mgr.get_stats()
    assert stats["due_count"] == 3
    assert stats["planned_read_calls"] == 3
    assert stats["read_calls"] == 1
    assert stats["sampled_count"] == 1
    assert stats["skipped_count"] == 2
    assert stats["budget_limited"] is True
    assert stats["budget_reason"] == "read_calls"
    items = {item["name"]: item for item in mgr.list_items()}
    assert items["a"]["read_count"] == 1
    assert items["b"]["read_count"] == 0
    assert items["c"]["read_count"] == 0


def test_memory_watch_manager_budget_can_be_reconfigured():
    mgr = MemoryWatchManager(
        read_memory=lambda address, size: bytes(size),
        on_update=lambda items: None,
    )

    budget = mgr.set_budget(
        max_read_calls_per_cycle="0x2",
        max_bytes_per_cycle="0x20",
        max_cycle_ms="0",
        merge_gap="4",
    )

    assert budget == {
        "max_read_calls_per_cycle": 2,
        "max_bytes_per_cycle": 32,
        "max_cycle_ms": 0.0,
        "merge_gap": 4,
    }
    assert mgr.get_stats()["budget"] == budget

    with pytest.raises(WatchError):
        mgr.set_budget(max_bytes_per_cycle=0)


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
