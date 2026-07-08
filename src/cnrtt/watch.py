"""Memory watch support for runtime J-Link variable sampling."""

from __future__ import annotations

import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


class WatchError(Exception):
    """Raised for memory watch configuration or sampling errors."""


VALUE_TYPES = {
    "u8": {"size": 1, "kind": "int", "signed": False},
    "u16": {"size": 2, "kind": "int", "signed": False},
    "u32": {"size": 4, "kind": "int", "signed": False},
    "u64": {"size": 8, "kind": "int", "signed": False},
    "s8": {"size": 1, "kind": "int", "signed": True},
    "s16": {"size": 2, "kind": "int", "signed": True},
    "s32": {"size": 4, "kind": "int", "signed": True},
    "s64": {"size": 8, "kind": "int", "signed": True},
    "float": {"size": 4, "kind": "float", "fmt": "<f"},
    "double": {"size": 8, "kind": "float", "fmt": "<d"},
}

TYPE_ALIASES = {
    "uint8_t": "u8",
    "uint16_t": "u16",
    "uint32_t": "u32",
    "uint64_t": "u64",
    "int8_t": "s8",
    "int16_t": "s16",
    "int32_t": "s32",
    "int64_t": "s64",
    "unsigned char": "u8",
    "unsigned short": "u16",
    "unsigned int": "u32",
    "unsigned long": "u32",
    "unsigned long long": "u64",
    "signed char": "s8",
    "short": "s16",
    "int": "s32",
    "long": "s32",
    "long long": "s64",
}

SHF_ALLOC = 0x2


def normalize_value_type(value_type: str) -> str:
    value = str(value_type or "").strip()
    if not value:
        return "u32"
    lowered = value.lower()
    normalized = TYPE_ALIASES.get(lowered, lowered)
    if normalized not in VALUE_TYPES:
        raise WatchError(f"不支持的变量类型: {value_type}")
    return normalized


def value_type_size(value_type: str) -> int:
    return int(VALUE_TYPES[normalize_value_type(value_type)]["size"])


def format_watch_value(raw: bytes, value_type: str) -> str:
    value_type = normalize_value_type(value_type)
    spec = VALUE_TYPES[value_type]
    size = int(spec["size"])
    if len(raw) < size:
        raise WatchError(f"读取字节数不足: {len(raw)}/{size}")
    data = raw[:size]
    if spec["kind"] == "float":
        return f"{struct.unpack(str(spec['fmt']), data)[0]:.6g}"

    signed = bool(spec["signed"])
    value = int.from_bytes(data, byteorder="little", signed=signed)
    raw_value = int.from_bytes(data, byteorder="little", signed=False)
    hex_width = size * 2
    return f"{value} (0x{raw_value:0{hex_width}X})"


def parse_address(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip().replace("_", "")
    if not text:
        raise WatchError("地址不能为空")
    try:
        return int(text, 0)
    except ValueError as e:
        raise WatchError(f"地址格式错误: {value}") from e


@dataclass
class WatchItem:
    name: str
    address: int
    value_type: str = "u32"
    period_ms: int = 500
    enabled: bool = True
    source: str = "manual"
    item_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    last_value: str = ""
    last_error: str = ""
    updated_at: float = 0.0
    next_due: float = 0.0

    def __post_init__(self):
        self.name = str(self.name or "").strip() or f"0x{self.address:08X}"
        self.address = parse_address(self.address)
        self.value_type = normalize_value_type(self.value_type)
        self.period_ms = max(50, int(self.period_ms or 500))
        self.enabled = bool(self.enabled)
        self.source = str(self.source or "manual")

    def to_dict(self, include_runtime: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.item_id,
            "name": self.name,
            "address": self.address,
            "address_hex": f"0x{self.address:08X}",
            "type": self.value_type,
            "period_ms": self.period_ms,
            "enabled": self.enabled,
            "source": self.source,
        }
        if include_runtime:
            data.update(
                {
                    "value": self.last_value,
                    "error": self.last_error,
                    "updated_at": self.updated_at,
                }
            )
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WatchItem":
        return cls(
            item_id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or ""),
            address=parse_address(data.get("address", data.get("address_hex", 0))),
            value_type=str(data.get("type") or data.get("value_type") or "u32"),
            period_ms=int(data.get("period_ms") or 500),
            enabled=bool(data.get("enabled", True)),
            source=str(data.get("source") or "manual"),
        )


@dataclass
class AxfSymbol:
    name: str
    address: int
    value_type: str = "u32"
    size: int = 4

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "address_hex": f"0x{self.address:08X}",
            "type": self.value_type,
            "size": self.size,
        }


class MemoryWatchManager:
    """Polls configured variables through a supplied memory reader."""

    def __init__(
        self,
        read_memory: Callable[[int, int], bytes],
        on_update: Callable[[List[Dict[str, Any]]], None],
    ) -> None:
        self._read_memory = read_memory
        self._on_update = on_update
        self._lock = threading.RLock()
        self._items: Dict[str, WatchItem] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def add_item(
        self,
        name: str,
        address: Any,
        value_type: str = "u32",
        period_ms: int = 500,
        enabled: bool = True,
        source: str = "manual",
    ) -> Dict[str, Any]:
        item = WatchItem(
            name=name,
            address=parse_address(address),
            value_type=value_type,
            period_ms=period_ms,
            enabled=enabled,
            source=source,
        )
        with self._lock:
            item.next_due = time.monotonic()
            self._items[item.item_id] = item
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)
        return item.to_dict()

    def replace_items(self, items: List[Dict[str, Any]]) -> None:
        parsed = [WatchItem.from_dict(item) for item in items or []]
        with self._lock:
            self._items = {item.item_id: item for item in parsed}
            now = time.monotonic()
            for item in self._items.values():
                item.next_due = now
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def remove_item(self, item_id: str) -> None:
        with self._lock:
            self._items.pop(str(item_id), None)
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def clear_items(self) -> None:
        with self._lock:
            self._items.clear()
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def set_item_enabled(self, item_id: str, enabled: bool) -> None:
        with self._lock:
            item = self._items.get(str(item_id))
            if item:
                item.enabled = bool(enabled)
                item.next_due = time.monotonic()
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def list_items(self, include_runtime: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            return [item.to_dict(include_runtime=include_runtime) for item in self._items.values()]

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return True
            self._stop_event.clear()
            self._running = True
            now = time.monotonic()
            for item in self._items.values():
                item.next_due = now
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)
        return True

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._running = False
            self._stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            self._thread = None
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def _loop(self) -> None:
        while not self._stop_event.wait(0.05):
            now = time.monotonic()
            with self._lock:
                due_items = [
                    item
                    for item in self._items.values()
                    if item.enabled and item.next_due <= now
                ]

            if not due_items:
                continue

            changed = False
            for item in due_items:
                try:
                    raw = self._read_memory(item.address, value_type_size(item.value_type))
                    value = format_watch_value(raw, item.value_type)
                    error = ""
                except Exception as e:
                    value = ""
                    error = str(e)

                with self._lock:
                    current = self._items.get(item.item_id)
                    if not current:
                        continue
                    current.last_value = value
                    current.last_error = error
                    current.updated_at = time.time()
                    current.next_due = time.monotonic() + current.period_ms / 1000.0
                    changed = True

            if changed:
                self._emit_snapshot()

    def _emit_snapshot(self) -> None:
        with self._lock:
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def _snapshot_locked(self) -> List[Dict[str, Any]]:
        return [item.to_dict(include_runtime=True) for item in self._items.values()]


def load_axf_symbols(path: str) -> List[Dict[str, Any]]:
    """Load scalar variable candidates from a Keil/AC6 AXF file."""
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError as e:
        return _load_elf_symtab_symbols_raw(path)

    symbols: Dict[str, AxfSymbol] = {}
    with open(path, "rb") as f:
        elf = ELFFile(f)
        if elf.has_dwarf_info():
            _load_dwarf_symbols(elf, symbols)
        _load_symtab_symbols(elf, symbols)
    return [sym.to_dict() for sym in sorted(symbols.values(), key=lambda item: item.name)]


def _load_elf_symtab_symbols_raw(path: str) -> List[Dict[str, Any]]:
    """Minimal ELF .symtab parser used when pyelftools is not installed."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 16 or data[:4] != b"\x7fELF":
        raise WatchError("不是有效的 ELF/AXF 文件")

    elf_class = data[4]
    endian_id = data[5]
    if elf_class not in (1, 2) or endian_id not in (1, 2):
        raise WatchError("不支持的 ELF/AXF 格式")
    endian = "<" if endian_id == 1 else ">"

    if elf_class == 1:
        header_fmt = endian + "HHIIIIIHHHHHH"
        header_size = struct.calcsize(header_fmt)
        if len(data) < 16 + header_size:
            raise WatchError("ELF 头不完整")
        header = struct.unpack_from(header_fmt, data, 16)
        e_shoff, e_shentsize, e_shnum = header[5], header[10], header[11]
        sh_fmt = endian + "IIIIIIIIII"
        sym_fmt = endian + "IIIBBH"
    else:
        header_fmt = endian + "HHIQQQIHHHHHH"
        header_size = struct.calcsize(header_fmt)
        if len(data) < 16 + header_size:
            raise WatchError("ELF 头不完整")
        header = struct.unpack_from(header_fmt, data, 16)
        e_shoff, e_shentsize, e_shnum = header[5], header[10], header[11]
        sh_fmt = endian + "IIQQQQIIQQ"
        sym_fmt = endian + "IBBHQQ"

    sh_size = struct.calcsize(sh_fmt)
    if e_shoff <= 0 or e_shnum <= 0:
        raise WatchError("ELF 文件没有 section header")

    sections = []
    for index in range(e_shnum):
        offset = e_shoff + index * e_shentsize
        if offset + sh_size > len(data):
            break
        fields = struct.unpack_from(sh_fmt, data, offset)
        if elf_class == 1:
            section = {
                "type": fields[1],
                "flags": fields[2],
                "address": fields[3],
                "offset": fields[4],
                "size": fields[5],
                "link": fields[6],
                "entsize": fields[9],
            }
        else:
            section = {
                "type": fields[1],
                "flags": fields[2],
                "address": fields[3],
                "offset": fields[4],
                "size": fields[5],
                "link": fields[6],
                "entsize": fields[9],
            }
        sections.append(section)

    symbols: Dict[str, AxfSymbol] = {}
    for section in sections:
        if section["type"] != 2:  # SHT_SYMTAB
            continue
        strtab_index = int(section["link"])
        if strtab_index < 0 or strtab_index >= len(sections):
            continue
        strtab = sections[strtab_index]
        strings = data[strtab["offset"] : strtab["offset"] + strtab["size"]]
        entry_size = int(section["entsize"] or struct.calcsize(sym_fmt))
        count = int(section["size"] // entry_size) if entry_size else 0
        for idx in range(count):
            offset = section["offset"] + idx * entry_size
            if offset + struct.calcsize(sym_fmt) > len(data):
                continue
            fields = struct.unpack_from(sym_fmt, data, offset)
            if elf_class == 1:
                st_name, st_value, st_size, st_info, st_shndx = (
                    fields[0],
                    fields[1],
                    fields[2],
                    fields[3],
                    fields[5],
                )
            else:
                st_name, st_info, st_shndx, st_value, st_size = (
                    fields[0],
                    fields[1],
                    fields[3],
                    fields[4],
                    fields[5],
                )
            if st_info & 0x0F != 1:  # STT_OBJECT
                continue
            if not _raw_symbol_section_is_allocated(sections, int(st_shndx)):
                continue
            name = _read_c_string(strings, int(st_name))
            if not name or int(st_value) == 0:
                continue
            size = int(st_size or 4)
            symbols[name] = AxfSymbol(
                name=name,
                address=int(st_value),
                value_type=_size_to_default_type(size),
                size=size,
            )

    return [sym.to_dict() for sym in sorted(symbols.values(), key=lambda item: item.name)]


def _raw_symbol_section_is_allocated(sections: List[Dict[str, Any]], index: int) -> bool:
    if index <= 0 or index >= len(sections):
        return False
    return bool(int(sections[index].get("flags", 0)) & SHF_ALLOC)


def _read_c_string(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


def _load_dwarf_symbols(elf, symbols: Dict[str, AxfSymbol]) -> None:
    dwarf = elf.get_dwarf_info()
    address_size = int(getattr(elf.elfclass, "value", elf.elfclass) // 8) if hasattr(elf, "elfclass") else 4
    for cu in dwarf.iter_CUs():
        die_by_offset = {die.offset: die for die in cu.iter_DIEs()}
        for die in die_by_offset.values():
            if die.tag != "DW_TAG_variable":
                continue
            name_attr = die.attributes.get("DW_AT_name")
            loc_attr = die.attributes.get("DW_AT_location")
            if not name_attr or not loc_attr:
                continue
            address = _location_to_address(loc_attr.value, address_size)
            if address is None:
                continue
            if not _address_is_in_allocated_section(elf, address):
                continue
            name = _decode_attr(name_attr.value)
            value_type, size = _dwarf_type_to_watch(cu, die_by_offset, die)
            if value_type:
                symbols[name] = AxfSymbol(name=name, address=address, value_type=value_type, size=size)


def _load_symtab_symbols(elf, symbols: Dict[str, AxfSymbol]) -> None:
    for section in elf.iter_sections():
        if not hasattr(section, "iter_symbols"):
            continue
        for sym in section.iter_symbols():
            info = sym["st_info"]
            if info["type"] != "STT_OBJECT":
                continue
            name = sym.name
            address = int(sym["st_value"])
            size = int(sym["st_size"] or 4)
            if not name or address == 0:
                continue
            if not _symbol_section_is_allocated(elf, sym):
                continue
            if name not in symbols:
                symbols[name] = AxfSymbol(
                    name=name,
                    address=address,
                    value_type=_size_to_default_type(size),
                    size=size,
                )


def _symbol_section_is_allocated(elf, sym) -> bool:
    section_index = sym["st_shndx"]
    if not isinstance(section_index, int):
        return False
    try:
        section = elf.get_section(section_index)
    except Exception:
        return False
    if section is None:
        return False
    return bool(int(section["sh_flags"]) & SHF_ALLOC)


def _address_is_in_allocated_section(elf, address: int) -> bool:
    for section in elf.iter_sections():
        try:
            if not int(section["sh_flags"]) & SHF_ALLOC:
                continue
            start = int(section["sh_addr"])
            size = int(section["sh_size"])
        except Exception:
            continue
        if size > 0 and start <= address < start + size:
            return True
    return False


def _decode_attr(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _location_to_address(expr, address_size: int) -> Optional[int]:
    if isinstance(expr, int):
        # DWARF location-list offset; first phase only supports direct DW_OP_addr.
        return None
    data = bytes(expr)
    if not data or data[0] != 0x03:  # DW_OP_addr
        return None
    size = max(1, int(address_size or 4))
    if len(data) < 1 + size:
        return None
    return int.from_bytes(data[1 : 1 + size], byteorder="little", signed=False)


def _dwarf_type_to_watch(cu, die_by_offset, die) -> Tuple[Optional[str], int]:
    type_die = _resolve_type_die(cu, die_by_offset, die)
    if type_die is None:
        return None, 0
    return _type_die_to_watch(cu, die_by_offset, type_die)


def _resolve_type_die(cu, die_by_offset, die):
    attr = die.attributes.get("DW_AT_type")
    if not attr:
        return None
    if getattr(attr, "form", "") == "DW_FORM_ref_addr":
        offset = attr.value
    else:
        offset = cu.cu_offset + attr.value
    return die_by_offset.get(offset)


def _type_die_to_watch(cu, die_by_offset, die) -> Tuple[Optional[str], int]:
    while die is not None and die.tag in (
        "DW_TAG_typedef",
        "DW_TAG_const_type",
        "DW_TAG_volatile_type",
        "DW_TAG_restrict_type",
    ):
        next_die = _resolve_type_die(cu, die_by_offset, die)
        if next_die is None:
            break
        die = next_die

    if die is None:
        return None, 0
    if die.tag == "DW_TAG_base_type":
        size_attr = die.attributes.get("DW_AT_byte_size")
        encoding_attr = die.attributes.get("DW_AT_encoding")
        if not size_attr or not encoding_attr:
            return None, 0
        size = int(size_attr.value)
        encoding = int(encoding_attr.value)
        # DW_ATE_address=1, boolean=2, float=4, signed=5, signed_char=6,
        # unsigned=7, unsigned_char=8.
        if encoding == 4:
            if size == 4:
                return "float", size
            if size == 8:
                return "double", size
        if encoding in (5, 6):
            return _size_to_default_type(size, signed=True), size
        if encoding in (1, 2, 7, 8):
            return _size_to_default_type(size, signed=False), size
    return None, 0


def _size_to_default_type(size: int, signed: bool = False) -> str:
    prefix = "s" if signed else "u"
    if size <= 1:
        return prefix + "8"
    if size <= 2:
        return prefix + "16"
    if size <= 4:
        return prefix + "32"
    return prefix + "64"
