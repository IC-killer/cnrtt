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
MAX_DWARF_ARRAY_ELEMENTS = 256
MAX_DWARF_EXPANSION_DEPTH = 6
DEFAULT_WATCH_MAX_READ_CALLS = 64
DEFAULT_WATCH_MAX_BYTES = 16 * 1024
DEFAULT_WATCH_MAX_CYCLE_MS = 25.0
DEFAULT_WATCH_MERGE_GAP = 16


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
    last_raw: str = ""
    last_error: str = ""
    updated_at: float = 0.0
    latency_ms: float = 0.0
    read_count: int = 0
    fail_count: int = 0
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
                    "raw": self.last_raw,
                    "error": self.last_error,
                    "updated_at": self.updated_at,
                    "latency_ms": self.latency_ms,
                    "read_count": self.read_count,
                    "fail_count": self.fail_count,
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
class _WatchRead:
    item_id: str
    address: int
    size: int
    value_type: str
    period_ms: int


@dataclass
class _WatchReadBatch:
    address: int
    end: int
    reads: List[_WatchRead] = field(default_factory=list)

    @property
    def size(self) -> int:
        return max(0, self.end - self.address)

    def add(self, read: _WatchRead) -> None:
        self.reads.append(read)
        self.end = max(self.end, read.address + read.size)


@dataclass
class AxfSymbol:
    name: str
    address: int
    value_type: str = "u32"
    size: int = 4
    kind: str = "scalar"
    parent: str = ""
    path: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "name": self.name,
            "address": self.address,
            "address_hex": f"0x{self.address:08X}",
            "type": self.value_type,
            "size": self.size,
        }
        if self.kind and self.kind != "scalar":
            data["kind"] = self.kind
        if self.parent:
            data["parent"] = self.parent
        if self.path:
            data["path"] = self.path
        if self.detail:
            data["detail"] = self.detail
        return data


class MemoryWatchManager:
    """Polls configured variables through a supplied memory reader."""

    def __init__(
        self,
        read_memory: Callable[[int, int], bytes],
        on_update: Callable[[List[Dict[str, Any]]], None],
        max_read_calls_per_cycle: int = DEFAULT_WATCH_MAX_READ_CALLS,
        max_bytes_per_cycle: int = DEFAULT_WATCH_MAX_BYTES,
        max_cycle_ms: float = DEFAULT_WATCH_MAX_CYCLE_MS,
        merge_gap: int = DEFAULT_WATCH_MERGE_GAP,
    ) -> None:
        self._read_memory = read_memory
        self._on_update = on_update
        self._lock = threading.RLock()
        self._items: Dict[str, WatchItem] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._max_read_calls_per_cycle = max(1, int(max_read_calls_per_cycle or 1))
        self._max_bytes_per_cycle = max(1, int(max_bytes_per_cycle or 1))
        self._max_cycle_ms = max(0.0, float(max_cycle_ms or 0.0))
        self._merge_gap = max(0, int(merge_gap or 0))
        self._stats: Dict[str, Any] = self._empty_stats()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    @staticmethod
    def _empty_stats() -> Dict[str, Any]:
        return {
            "last_cycle_at": 0.0,
            "duration_ms": 0.0,
            "due_count": 0,
            "sampled_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "planned_read_calls": 0,
            "read_calls": 0,
            "bytes_requested": 0,
            "merge_saved_calls": 0,
            "merge_ratio": 0.0,
            "budget_limited": False,
            "budget_reason": "",
        }

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

            changed = self._sample_due_items(due_items)
            if changed:
                self._emit_snapshot()

    def _sample_due_items(self, due_items: List[WatchItem]) -> bool:
        cycle_started = time.monotonic()
        reads = [
            _WatchRead(
                item_id=item.item_id,
                address=item.address,
                size=value_type_size(item.value_type),
                value_type=item.value_type,
                period_ms=item.period_ms,
            )
            for item in due_items
        ]
        batches = self._build_read_batches(reads)
        stats = self._empty_stats()
        stats["last_cycle_at"] = time.time()
        stats["due_count"] = len(reads)
        stats["planned_read_calls"] = len(batches)
        stats["merge_saved_calls"] = max(0, len(reads) - len(batches))
        if reads:
            stats["merge_ratio"] = round(stats["merge_saved_calls"] * 100.0 / len(reads), 1)

        changed = False
        for batch_index, batch in enumerate(batches):
            budget_reason = self._budget_reason(stats, batch, cycle_started)
            if budget_reason:
                skipped = self._skip_batches(batches[batch_index:])
                stats["skipped_count"] += skipped
                stats["budget_limited"] = True
                stats["budget_reason"] = budget_reason
                changed = True
                break

            started = time.monotonic()
            stats["read_calls"] += 1
            stats["bytes_requested"] += batch.size
            try:
                raw = bytes(self._read_memory(batch.address, batch.size))
                if len(raw) < batch.size:
                    raise WatchError(f"读取字节数不足: {len(raw)}/{batch.size}")
                latency_ms = (time.monotonic() - started) * 1000.0
                sampled, failed = self._apply_batch_data(batch, raw, latency_ms)
            except Exception as e:
                latency_ms = (time.monotonic() - started) * 1000.0
                sampled, failed = 0, self._apply_batch_error(batch, str(e), latency_ms)
            stats["sampled_count"] += sampled
            stats["failed_count"] += failed
            changed = True

        stats["duration_ms"] = round((time.monotonic() - cycle_started) * 1000.0, 3)
        with self._lock:
            self._stats = stats
        return changed

    def _build_read_batches(self, reads: List[_WatchRead]) -> List[_WatchReadBatch]:
        batches: List[_WatchReadBatch] = []
        for read in sorted(reads, key=lambda item: (item.address, item.size)):
            if not batches:
                batches.append(
                    _WatchReadBatch(
                        address=read.address,
                        end=read.address + read.size,
                        reads=[read],
                    )
                )
                continue
            current = batches[-1]
            proposed_end = max(current.end, read.address + read.size)
            proposed_size = proposed_end - current.address
            if read.address <= current.end + self._merge_gap and proposed_size <= self._max_bytes_per_cycle:
                current.add(read)
            else:
                batches.append(
                    _WatchReadBatch(
                        address=read.address,
                        end=read.address + read.size,
                        reads=[read],
                    )
                )
        return batches

    def _budget_reason(
        self,
        stats: Dict[str, Any],
        batch: _WatchReadBatch,
        cycle_started: float,
    ) -> str:
        if int(stats["read_calls"]) >= self._max_read_calls_per_cycle:
            return "read_calls"
        if int(stats["bytes_requested"]) + batch.size > self._max_bytes_per_cycle:
            return "bytes"
        if self._max_cycle_ms > 0:
            elapsed_ms = (time.monotonic() - cycle_started) * 1000.0
            if elapsed_ms >= self._max_cycle_ms:
                return "time"
        return ""

    def _apply_batch_data(
        self,
        batch: _WatchReadBatch,
        raw: bytes,
        latency_ms: float,
    ) -> Tuple[int, int]:
        sampled = 0
        failed = 0
        with self._lock:
            for read in batch.reads:
                current = self._items.get(read.item_id)
                if not current:
                    continue
                offset = read.address - batch.address
                chunk = raw[offset : offset + read.size]
                try:
                    value = format_watch_value(chunk, read.value_type)
                    current.last_value = value
                    current.last_raw = _format_raw_bytes(chunk)
                    current.last_error = ""
                    sampled += 1
                except Exception as e:
                    current.last_error = str(e)
                    current.fail_count += 1
                    failed += 1
                current.read_count += 1
                current.latency_ms = round(latency_ms, 3)
                current.updated_at = time.time()
                current.next_due = time.monotonic() + current.period_ms / 1000.0
        return sampled, failed

    def _apply_batch_error(
        self,
        batch: _WatchReadBatch,
        error: str,
        latency_ms: float,
    ) -> int:
        failed = 0
        with self._lock:
            for read in batch.reads:
                current = self._items.get(read.item_id)
                if not current:
                    continue
                current.read_count += 1
                current.fail_count += 1
                current.latency_ms = round(latency_ms, 3)
                current.last_error = error
                current.updated_at = time.time()
                current.next_due = time.monotonic() + current.period_ms / 1000.0
                failed += 1
        return failed

    def _skip_batches(self, batches: List[_WatchReadBatch]) -> int:
        skipped = 0
        with self._lock:
            now = time.monotonic()
            for batch in batches:
                for read in batch.reads:
                    current = self._items.get(read.item_id)
                    if not current:
                        continue
                    current.next_due = now + current.period_ms / 1000.0
                    skipped += 1
        return skipped

    def _emit_snapshot(self) -> None:
        with self._lock:
            snapshot = self._snapshot_locked()
        self._on_update(snapshot)

    def _snapshot_locked(self) -> List[Dict[str, Any]]:
        return [item.to_dict(include_runtime=True) for item in self._items.values()]


def _format_raw_bytes(raw: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in bytes(raw))


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
            address = _location_attr_to_address(dwarf, die, loc_attr, address_size)
            if address is None:
                continue
            if not _address_is_in_allocated_section(elf, address):
                continue
            name = _decode_attr(name_attr.value)
            type_die = _resolve_type_die(cu, die_by_offset, die)
            if type_die is None:
                continue
            _add_dwarf_watch_symbols(
                cu=cu,
                die_by_offset=die_by_offset,
                symbols=symbols,
                name=name,
                address=address,
                type_die=type_die,
                address_size=address_size,
            )


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
            if _has_expanded_symbol(symbols, name):
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


def _attr_value(die, name: str, default: Any = None) -> Any:
    attr = die.attributes.get(name)
    if attr is None:
        return default
    return getattr(attr, "value", attr)


def _attr_int(die, name: str, default: Optional[int] = None) -> Optional[int]:
    value = _attr_value(die, name, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _die_name(die, default: str = "") -> str:
    value = _attr_value(die, "DW_AT_name", default)
    if value is None:
        return default
    return _decode_attr(value)


def _has_expanded_symbol(symbols: Dict[str, AxfSymbol], name: str) -> bool:
    prefix_dot = f"{name}."
    prefix_bracket = f"{name}["
    return any(
        item_name.startswith(prefix_dot) or item_name.startswith(prefix_bracket)
        for item_name in symbols
    )


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


def _location_attr_to_address(dwarf, die, loc_attr, address_size: int) -> Optional[int]:
    address = _location_to_address(getattr(loc_attr, "value", loc_attr), address_size)
    if address is not None:
        return address

    offset = getattr(loc_attr, "value", loc_attr)
    if not isinstance(offset, int):
        return None
    try:
        loc_lists = dwarf.location_lists()
    except Exception:
        return None
    try:
        entries = loc_lists.get_location_list_at_offset(offset, die=die)
    except TypeError:
        try:
            entries = loc_lists.get_location_list_at_offset(offset)
        except Exception:
            return None
    except Exception:
        return None

    for entry in entries or []:
        expr = getattr(entry, "loc_expr", None)
        if expr is None and isinstance(entry, dict):
            expr = entry.get("loc_expr")
        if expr is None:
            continue
        address = _location_to_address(expr, address_size)
        if address is not None:
            return address
    return None


def _dwarf_type_to_watch(cu, die_by_offset, die) -> Tuple[Optional[str], int]:
    type_die = _resolve_type_die(cu, die_by_offset, die)
    if type_die is None:
        return None, 0
    info = _type_die_to_watch_info(cu, die_by_offset, type_die, 4)
    if info and info.get("watch_type"):
        return str(info["watch_type"]), int(info.get("size", 0))
    return None, 0


def _resolve_type_die(cu, die_by_offset, die):
    attr = die.attributes.get("DW_AT_type")
    if not attr:
        return None
    if getattr(attr, "form", "") == "DW_FORM_ref_addr":
        offset = attr.value
    else:
        offset = cu.cu_offset + attr.value
    return die_by_offset.get(offset)


def _unwrap_type_die(cu, die_by_offset, die):
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
    return die


def _type_die_to_watch(cu, die_by_offset, die) -> Tuple[Optional[str], int]:
    info = _type_die_to_watch_info(cu, die_by_offset, die, 4)
    if info and info.get("watch_type"):
        return str(info["watch_type"]), int(info.get("size", 0))
    return None, 0


def _type_die_to_watch_info(
    cu,
    die_by_offset,
    die,
    address_size: int,
    seen: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    seen = set() if seen is None else set(seen)
    die = _unwrap_type_die(cu, die_by_offset, die)

    if die is None:
        return None
    die_offset = getattr(die, "offset", None)
    if die_offset is not None:
        if die_offset in seen:
            return None
        seen.add(die_offset)

    if die.tag == "DW_TAG_base_type":
        size = _attr_int(die, "DW_AT_byte_size", 0) or 0
        encoding = _attr_int(die, "DW_AT_encoding", 0) or 0
        if size <= 0 or encoding <= 0:
            return None
        # DW_ATE_address=1, boolean=2, float=4, signed=5, signed_char=6,
        # unsigned=7, unsigned_char=8.
        if encoding == 4:
            if size == 4:
                return {"kind": "scalar", "watch_type": "float", "size": size}
            if size == 8:
                return {"kind": "scalar", "watch_type": "double", "size": size}
        if encoding in (5, 6):
            return {
                "kind": "scalar",
                "watch_type": _size_to_default_type(size, signed=True),
                "size": size,
            }
        if encoding in (1, 2, 7, 8):
            return {
                "kind": "scalar",
                "watch_type": _size_to_default_type(size, signed=False),
                "size": size,
            }
        return None

    if die.tag == "DW_TAG_enumeration_type":
        size = _attr_int(die, "DW_AT_byte_size", 4) or 4
        values = _enum_values(die)
        signed = any(value < 0 for value in values.values())
        return {
            "kind": "enum",
            "watch_type": _size_to_default_type(size, signed=signed),
            "size": size,
            "enum": values,
            "type_name": _die_name(die),
        }

    if die.tag == "DW_TAG_pointer_type":
        size = _attr_int(die, "DW_AT_byte_size", address_size) or address_size
        target_die = _resolve_type_die(cu, die_by_offset, die)
        return {
            "kind": "pointer",
            "watch_type": _size_to_default_type(size, signed=False),
            "size": size,
            "target": _die_name(_unwrap_type_die(cu, die_by_offset, target_die), "")
            if target_die is not None
            else "",
        }

    if die.tag == "DW_TAG_array_type":
        element_die = _resolve_type_die(cu, die_by_offset, die)
        element = _type_die_to_watch_info(
            cu, die_by_offset, element_die, address_size, seen
        ) if element_die is not None else None
        counts = _array_dimension_counts(die)
        if not element or not counts:
            return None
        element_size = int(element.get("size", 0))
        total_count = 1
        for count in counts:
            total_count *= count
        if element_size <= 0 or total_count <= 0:
            return None
        return {
            "kind": "array",
            "size": element_size * total_count,
            "element": element,
            "element_size": element_size,
            "counts": counts,
        }

    if die.tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_class_type"):
        fields = []
        for child in _iter_die_children(die):
            if child.tag != "DW_TAG_member":
                continue
            if "DW_AT_bit_size" in child.attributes:
                continue
            field_name = _die_name(child)
            if not field_name:
                continue
            field_offset = _member_offset(child)
            if field_offset is None:
                continue
            field_die = _resolve_type_die(cu, die_by_offset, child)
            field_info = _type_die_to_watch_info(
                cu, die_by_offset, field_die, address_size, seen
            ) if field_die is not None else None
            if not field_info:
                continue
            fields.append(
                {
                    "name": field_name,
                    "offset": field_offset,
                    "type": field_info,
                }
            )
        if not fields:
            return None
        return {
            "kind": "union" if die.tag == "DW_TAG_union_type" else "struct",
            "size": _attr_int(die, "DW_AT_byte_size", 0) or 0,
            "type_name": _die_name(die),
            "fields": fields,
        }
    return None


def _add_dwarf_watch_symbols(
    cu,
    die_by_offset,
    symbols: Dict[str, AxfSymbol],
    name: str,
    address: int,
    type_die,
    address_size: int,
) -> int:
    info = _type_die_to_watch_info(cu, die_by_offset, type_die, address_size)
    if not info:
        return 0
    return _expand_watch_type_symbols(
        symbols=symbols,
        name=name,
        address=address,
        info=info,
        parent=name,
        path=name,
        depth=0,
    )


def _expand_watch_type_symbols(
    symbols: Dict[str, AxfSymbol],
    name: str,
    address: int,
    info: Dict[str, Any],
    parent: str,
    path: str,
    depth: int,
) -> int:
    if depth > MAX_DWARF_EXPANSION_DEPTH:
        return 0

    watch_type = info.get("watch_type")
    if watch_type:
        detail = _symbol_detail(info)
        symbols[name] = AxfSymbol(
            name=name,
            address=address,
            value_type=str(watch_type),
            size=int(info.get("size", value_type_size(str(watch_type)))),
            kind=str(info.get("kind") or "scalar"),
            parent="" if name == parent else parent,
            path=path,
            detail=detail,
        )
        return 1

    kind = info.get("kind")
    if kind == "array":
        return _expand_array_symbols(
            symbols=symbols,
            name=name,
            address=address,
            info=info,
            parent=parent,
            path=path,
            depth=depth,
        )
    if kind in ("struct", "union"):
        added = 0
        for field in info.get("fields", []):
            field_name = f"{name}.{field['name']}"
            field_path = f"{path}.{field['name']}"
            added += _expand_watch_type_symbols(
                symbols=symbols,
                name=field_name,
                address=address + int(field.get("offset", 0)),
                info=field["type"],
                parent=parent,
                path=field_path,
                depth=depth + 1,
            )
        return added
    return 0


def _expand_array_symbols(
    symbols: Dict[str, AxfSymbol],
    name: str,
    address: int,
    info: Dict[str, Any],
    parent: str,
    path: str,
    depth: int,
) -> int:
    counts = list(info.get("counts") or [])
    element = info.get("element")
    element_size = int(info.get("element_size", 0))
    if not counts or not element or element_size <= 0:
        return 0
    total_count = 1
    for count in counts:
        total_count *= int(count)
    if total_count <= 0 or total_count > MAX_DWARF_ARRAY_ELEMENTS:
        return 0

    added = 0
    strides = _array_strides(counts, element_size)
    for indices in _array_indices(counts):
        offset = sum(index * stride for index, stride in zip(indices, strides))
        suffix = "".join(f"[{index}]" for index in indices)
        added += _expand_watch_type_symbols(
            symbols=symbols,
            name=f"{name}{suffix}",
            address=address + offset,
            info=element,
            parent=parent,
            path=f"{path}{suffix}",
            depth=depth + 1,
        )
    return added


def _array_strides(counts: List[int], element_size: int) -> List[int]:
    strides = []
    for index in range(len(counts)):
        stride = element_size
        for count in counts[index + 1 :]:
            stride *= int(count)
        strides.append(stride)
    return strides


def _array_indices(counts: List[int]) -> List[Tuple[int, ...]]:
    result: List[Tuple[int, ...]] = [()]
    for count in counts:
        result = [prefix + (idx,) for prefix in result for idx in range(int(count))]
    return result


def _symbol_detail(info: Dict[str, Any]) -> Dict[str, Any]:
    detail: Dict[str, Any] = {}
    if info.get("enum"):
        detail["enum"] = dict(info["enum"])
    if info.get("target"):
        detail["target"] = str(info["target"])
    if info.get("type_name"):
        detail["type_name"] = str(info["type_name"])
    return detail


def _iter_die_children(die) -> List[Any]:
    try:
        return list(die.iter_children())
    except Exception:
        return []


def _enum_values(die) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for child in _iter_die_children(die):
        if child.tag != "DW_TAG_enumerator":
            continue
        name = _die_name(child)
        value = _attr_int(child, "DW_AT_const_value", None)
        if name and value is not None:
            values[name] = value
    return values


def _array_dimension_counts(die) -> List[int]:
    counts: List[int] = []
    for child in _iter_die_children(die):
        if child.tag != "DW_TAG_subrange_type":
            continue
        count = _attr_int(child, "DW_AT_count", None)
        if count is None:
            lower = _attr_int(child, "DW_AT_lower_bound", 0) or 0
            upper = _attr_int(child, "DW_AT_upper_bound", None)
            if upper is None:
                continue
            count = upper - lower + 1
        if count is None or count <= 0:
            continue
        counts.append(int(count))
    return counts


def _member_offset(die) -> Optional[int]:
    value = _attr_value(die, "DW_AT_data_member_location", 0)
    if isinstance(value, int):
        return value
    try:
        data = bytes(value)
    except Exception:
        return None
    if not data:
        return 0
    if data[0] == 0x23:  # DW_OP_plus_uconst
        return _decode_uleb128(data, 1)[0]
    if data[0] == 0x10:  # DW_OP_constu
        return _decode_uleb128(data, 1)[0]
    return None


def _decode_uleb128(data: bytes, offset: int = 0) -> Tuple[int, int]:
    result = 0
    shift = 0
    index = offset
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, index


def _size_to_default_type(size: int, signed: bool = False) -> str:
    prefix = "s" if signed else "u"
    if size <= 1:
        return prefix + "8"
    if size <= 2:
        return prefix + "16"
    if size <= 4:
        return prefix + "32"
    return prefix + "64"
