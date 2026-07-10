"""RTTCore —— cnrtt 的纯逻辑核心层。

本模块不依赖 tkinter / GUI，提供连接管理、收发、配置与事件总线，
供 GUI（RTTViewerApp）和 AI agent 服务端（AgentServer）共享。

设计要点：
- 单一真实源：所有 RTT 状态只存在于 RTTCore 实例中。
- 事件总线：GUI / Agent 通过 subscribe 订阅 output / status / error / config 事件，
  回调统一在 core 的回调线程中调用；GUI 侧需自行 marshal 回主线程。
- 环形缓冲：守护线程把读取到的文本写入 deque，供 get_output 增量拉取。
- 配置分离：人工 GUI 配置存 ~/.cnrtt/rtt_history.json；
  agent 配置存 ~/.cnrtt/agent_config.json；agent 命令历史存 ~/.cnrtt/agent_history.json。
- 线程安全：connect/disconnect/send/config 操作均通过 self._lock 串行化。
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import pylink

from cnrtt.watch import MemoryWatchManager, WatchError, load_axf_symbols

# ── 配置文件路径 ──────────────────────────────────────────────
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".cnrtt")
GUI_CONFIG_FILE = os.path.join(CONFIG_DIR, "rtt_history.json")
AGENT_CONFIG_FILE = os.path.join(CONFIG_DIR, "agent_config.json")
AGENT_HISTORY_FILE = os.path.join(CONFIG_DIR, "agent_history.json")

# 默认配置
DEFAULT_DEVICE = "STM32F407VE"
DEFAULT_IFACE = "SWD"
DEFAULT_CHARSET = "UTF-8"

# J-Link 状态自检：低频检查，异常时自动重连
DEFAULT_HEALTH_CHECK_INTERVAL = 10.0
DEFAULT_HEALTH_RECONNECT_ATTEMPTS = 3
DEFAULT_HEALTH_RECONNECT_DELAY = 0.5
DEFAULT_HEALTH_OK_LOG_INTERVAL = 60.0

# 环形缓冲容量（字符数）
OUTPUT_BUFFER_SIZE = 64 * 1024
# agent 命令历史上限
AGENT_HISTORY_LIMIT = 1000
# 单次运行态内存读取上限，避免误操作造成长时间占用 J-Link IO 锁。
DEFAULT_MEMORY_READ_LIMIT = 64 * 1024
MEMORY_READ_ADDRESS_LIMIT = (1 << 64) - 1


class RTTError(Exception):
    """RTT 业务错误，携带可读消息；agent_server 会映射为 JSON-RPC 错误码。"""

    def __init__(self, message: str, kind: Optional[str] = None) -> None:
        super().__init__(message)
        self.kind = kind or "internal"


# ── 事件类型 ──────────────────────────────────────────────────
EVENT_OUTPUT = "output"      # text: str
EVENT_STATUS = "status"      # connected: bool
EVENT_CONFIG = "config"      # config: dict
EVENT_ERROR = "error"        # message: str
EVENT_WATCH = "watch"        # items: list


class RTTCore:
    """RTT 工具核心，零 GUI 依赖。

    线程模型：
    - 主线程：调用 connect/disconnect/send/set_config 等。
    - 守护线程 _read_loop：持续 rtt_read，把文本投递给事件总线 + 环形缓冲。
    - 事件回调：在调用 subscribe 的线程之外触发，订阅者需自行线程安全。
    """

    def __init__(
        self,
        output_buffer_size: int = OUTPUT_BUFFER_SIZE,
        read_interval: float = 0.01,
        send_retries: int = 20,
        health_check_interval: float = DEFAULT_HEALTH_CHECK_INTERVAL,
        health_reconnect_attempts: int = DEFAULT_HEALTH_RECONNECT_ATTEMPTS,
        memory_read_limit: int = DEFAULT_MEMORY_READ_LIMIT,
    ) -> None:
        self._lock = threading.RLock()
        self._jlink_io_lock = threading.RLock()
        self._recovery_lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self._subs: Dict[int, Callable[[str, Dict[str, Any]], None]] = {}
        self._next_sub_id = 1

        # RTT 连接状态
        self._jlink: Optional[Any] = None
        self._connected = False
        self._read_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._health_thread: Optional[threading.Thread] = None
        self._health_stop_event = threading.Event()
        self._jlink_status = "未连接"
        self._jlink_status_detail = "未连接"
        self._last_health_check = 0.0
        self._last_health_ok_log = 0.0

        # 配置（运行期可变）
        self._device = DEFAULT_DEVICE
        self._iface = DEFAULT_IFACE
        self._charset = DEFAULT_CHARSET
        self._echo_send = False
        self._hex_dump = False

        # GUI 配置（设备历史等）
        self._gui_history: Dict[str, Any] = {
            "last_device": DEFAULT_DEVICE,
            "devices": [DEFAULT_DEVICE],
        }

        # 输出环形缓冲：存 (seq, text)，seq 单调递增
        self._output_buf: Deque[Tuple[int, str]] = collections.deque(
            maxlen=output_buffer_size
        )
        self._output_seq = 0
        self._output_lock = threading.Lock()

        # 读取/发送参数
        self._read_interval = read_interval
        self._send_retries = send_retries
        self._health_check_interval = max(0.0, float(health_check_interval))
        self._health_reconnect_attempts = max(0, int(health_reconnect_attempts))
        self._health_reconnect_delay = DEFAULT_HEALTH_RECONNECT_DELAY
        self._health_ok_log_interval = DEFAULT_HEALTH_OK_LOG_INTERVAL
        self._memory_read_limit = max(1, int(memory_read_limit or DEFAULT_MEMORY_READ_LIMIT))
        self._memory_read_count = 0
        self._memory_read_fail_count = 0
        self._last_memory_read_at = 0.0
        self._last_memory_read_latency_ms = 0.0
        self._last_memory_read_error = ""
        self._last_memory_read_error_kind = ""

        # agent 命令历史（由 agent_server 维护，core 提供存取）
        self._agent_history: List[str] = []

        # 变量运行态监控（GUI 和 agent JSON-RPC 共享）
        self._watch_manager = MemoryWatchManager(
            read_memory=self.read_memory,
            on_update=self._emit_watch_update,
        )

    # ── 事件总线 ──────────────────────────────────────────────
    def subscribe(self, callback: Callable[[str, Dict[str, Any]], None]) -> int:
        """订阅事件。callback(event_type, payload)。返回 sub_id 用于取消。"""
        with self._sub_lock:
            sid = self._next_sub_id
            self._next_sub_id += 1
            self._subs[sid] = callback
            return sid

    def unsubscribe(self, sub_id: int) -> None:
        with self._sub_lock:
            self._subs.pop(sub_id, None)

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        """向所有订阅者广播事件。回调异常被吞掉，避免影响其他订阅者。"""
        with self._sub_lock:
            subs = list(self._subs.values())
        for cb in subs:
            try:
                cb(event_type, payload)
            except Exception:
                # 订阅者出错不应阻断 core；GUI 侧的 marshal 错误也忽略
                pass

    def _status_payload(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "jlink_status": self._jlink_status,
                "jlink_status_detail": self._jlink_status_detail,
                "last_health_check": self._last_health_check,
                "health_check_interval": self._health_check_interval,
                "health_reconnect_attempts": self._health_reconnect_attempts,
            }

    def _emit_status(self) -> None:
        self._emit(EVENT_STATUS, self._status_payload())

    # ── 连接 ──────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def connect(
        self,
        device: Optional[str] = None,
        iface: Optional[str] = None,
        charset: Optional[str] = None,
    ) -> bool:
        """连接 J-Link 并启动 RTT。失败抛 RTTError。成功返回 True。"""
        with self._lock:
            if self._connected:
                # 已连接则先断开，确保状态干净
                try:
                    self.stop_memory_watch()
                    self._disconnect_locked()
                except Exception:
                    pass

            if device:
                self._device = device
            if iface:
                self._iface = iface
            if charset:
                self._charset = charset

            return self._connect_locked(announce=True, start_health=True)

    def _connect_locked(
        self,
        announce: bool = True,
        start_health: bool = True,
    ) -> bool:
        """建立 J-Link/RTT 会话。调用方已持有 _lock。"""
        try:
            with self._jlink_io_lock:
                self._jlink = pylink.JLink()
                self._jlink.open()

                iface_enum = (
                    pylink.enums.JLinkInterfaces.SWD
                    if self._iface == "SWD"
                    else pylink.enums.JLinkInterfaces.JTAG
                )
                try:
                    self._jlink.set_tif(iface_enum)
                except AttributeError:
                    pass

                self._jlink.connect(self._device)
                self._jlink.rtt_start()
            time.sleep(0.2)

            self._connected = True
            self._stop_event.clear()
            self._set_jlink_status("已连接", "J-Link connected")
            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="cnrtt-rtt-read"
            )
            self._read_thread.start()
            if start_health:
                self._start_health_check_locked()

            if announce:
                self._emit_output("[系统] 连接成功，RTT 已启动。\n")
            self._emit_status()
            self._emit(
                EVENT_CONFIG,
                {
                    "device": self._device,
                    "iface": self._iface,
                    "charset": self._charset,
                    "echo_send": self._echo_send,
                    "hex_dump": self._hex_dump,
                },
            )
            return True
        except Exception as e:
            # 清理半开连接
            if self._jlink:
                try:
                    self._jlink.close()
                except Exception:
                    pass
                self._jlink = None
            self._connected = False
            self._set_jlink_status("连接失败", str(e))
            self._emit_status()
            if announce:
                msg = f"[系统错误] {e}\n"
                self._emit_output(msg)
                self._emit(EVENT_ERROR, {"message": str(e)})
            raise RTTError(str(e)) from e

    def disconnect(self) -> None:
        self.stop_memory_watch()
        with self._lock:
            self._disconnect_locked(stop_health=True)
        self._emit_output("[系统] 已断开连接。\n")
        self._emit_status()

    def _disconnect_locked(self, stop_health: bool = True) -> None:
        """断开（调用方已持锁）。"""
        if stop_health:
            self._stop_health_check_locked()
        self._connected = False
        self._set_jlink_status("未连接", "disconnected")
        self._stop_event.set()
        if (
            self._read_thread
            and self._read_thread.is_alive()
            and self._read_thread is not threading.current_thread()
        ):
            self._read_thread.join(timeout=1.0)
        self._read_thread = None

        with self._jlink_io_lock:
            if self._jlink:
                try:
                    if self._jlink.connected():
                        self._jlink.rtt_stop()
                    self._jlink.close()
                except Exception:
                    pass
                self._jlink = None

    # ── J-Link 状态自检 / 自动恢复 ───────────────────────────
    def _set_jlink_status(self, status: str, detail: str = "") -> None:
        self._jlink_status = status
        self._jlink_status_detail = detail
        self._last_health_check = time.time()

    def _start_health_check_locked(self) -> None:
        if self._health_check_interval <= 0:
            return
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_stop_event.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop,
            daemon=True,
            name="cnrtt-jlink-health",
        )
        self._health_thread.start()

    def _stop_health_check_locked(self) -> None:
        self._health_stop_event.set()
        thread = self._health_thread
        if (
            thread
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
        if thread is not threading.current_thread():
            self._health_thread = None

    def _health_loop(self) -> None:
        try:
            while not self._health_stop_event.wait(self._health_check_interval):
                self.check_jlink_status(recover=True)
        finally:
            with self._lock:
                if self._health_thread is threading.current_thread():
                    self._health_thread = None

    def _probe_jlink_status(self) -> Tuple[bool, str]:
        with self._jlink_io_lock:
            if not self._connected or not self._jlink:
                return False, "not connected"
            try:
                if hasattr(self._jlink, "connected") and not self._jlink.connected():
                    return False, "J-Link disconnected"
                target_connected = getattr(self._jlink, "target_connected", None)
                if callable(target_connected) and not target_connected():
                    return False, "target disconnected"
            except Exception as e:
                return False, str(e)
        return True, "J-Link connected"

    def check_jlink_status(self, recover: bool = True) -> bool:
        """低频 J-Link 自检。recover=True 时异常会同步尝试自动重连。"""
        with self._lock:
            has_session = self._connected or self._jlink is not None
        if not has_session:
            with self._lock:
                self._set_jlink_status("未连接", "not connected")
            self._emit_status()
            return False

        ok, detail = self._probe_jlink_status()
        now = time.monotonic()
        if ok:
            with self._lock:
                self._set_jlink_status("已连接", detail)
                if now - self._last_health_ok_log >= self._health_ok_log_interval:
                    self._last_health_ok_log = now
            self._emit_status()
            return True

        with self._lock:
            self._set_jlink_status("异常", detail)
        self._emit_status()
        if recover:
            return self._recover_connection(detail)
        self._emit(EVENT_ERROR, {"message": f"J-Link health check failed: {detail}"})
        return False

    def _schedule_recovery(self, reason: str) -> None:
        if self._health_reconnect_attempts <= 0 or self._recovery_lock.locked():
            return
        t = threading.Thread(
            target=self._recover_connection,
            args=(reason,),
            daemon=True,
            name="cnrtt-jlink-recover",
        )
        t.start()

    def _recover_connection(
        self,
        reason: str,
        attempts: Optional[int] = None,
    ) -> bool:
        attempts = self._health_reconnect_attempts if attempts is None else attempts
        if attempts <= 0:
            return False
        if not self._recovery_lock.acquire(blocking=False):
            return False

        try:
            with self._lock:
                has_session = self._connected or self._jlink is not None
                device = self._device
                iface = self._iface
                charset = self._charset
                watch_was_running = self.memory_watch_running()
                self._connected = False
                self._set_jlink_status("重连中", reason)
            if not has_session:
                return False

            self._emit_status()

            last_error: Optional[Exception] = None
            for attempt in range(1, attempts + 1):
                if self._health_stop_event.is_set():
                    return False
                try:
                    with self._lock:
                        self._set_jlink_status(
                            "重连中",
                            f"自动重连 {attempt}/{attempts}: {reason}",
                        )
                    self._emit_status()
                    with self._lock:
                        self.stop_memory_watch()
                        self._disconnect_locked(stop_health=False)
                        self._device = device
                        self._iface = iface
                        self._charset = charset
                        self._connect_locked(announce=False, start_health=True)
                    if watch_was_running:
                        try:
                            self.start_memory_watch()
                        except RTTError as e:
                            with self._lock:
                                self._set_jlink_status(
                                    "已连接",
                                    f"自动重连成功，变量采样恢复失败: {e}",
                                )
                            self._emit_status()
                            return True
                    with self._lock:
                        self._set_jlink_status("已连接", "自动重连成功")
                    self._emit_status()
                    return True
                except Exception as e:
                    last_error = e
                    with self._lock:
                        self._set_jlink_status(
                            "重连中",
                            f"自动重连 {attempt}/{attempts} 失败: {e}",
                        )
                    self._emit_status()
                    if attempt < attempts:
                        self._health_stop_event.wait(self._health_reconnect_delay)

            with self._lock:
                self.stop_memory_watch()
                self._disconnect_locked(stop_health=False)
                self._set_jlink_status(
                    "断开",
                    f"auto reconnect failed: {last_error}",
                )
                self._health_stop_event.set()
            self._emit_status()
            self._emit(
                EVENT_ERROR,
                {"message": f"auto reconnect failed: {last_error}"},
            )
            return False
        finally:
            self._recovery_lock.release()

    # ── 读取循环 ──────────────────────────────────────────────
    def _read_loop(self) -> None:
        """守护线程：持续读取 RTT 通道 0，写入缓冲并广播事件。"""
        charset = self._charset
        while not self._stop_event.is_set() and self._connected and self._jlink:
            try:
                with self._jlink_io_lock:
                    if not self._connected or not self._jlink:
                        break
                    data = self._jlink.rtt_read(0, 1024)
                if data:
                    byte_data = bytes(data)
                    if self._hex_dump:
                        hex_str = " ".join(f"{b:02X}" for b in byte_data)
                        self._emit_output(f"[HEX {len(byte_data)}B] {hex_str}\n")
                    text = byte_data.decode(charset, errors="replace")
                    self._emit_output(text)
            except Exception as e:
                if self._stop_event.is_set():
                    break
                self._emit_output(f"[读取错误] {e}\n")
                self._emit(EVENT_ERROR, {"message": f"read error: {e}"})
                # 读取异常视为连接断开
                self._connected = False
                with self._lock:
                    self._set_jlink_status("异常", f"read error: {e}")
                self.stop_memory_watch()
                self._emit_status()
                self._schedule_recovery(f"读取失败: {e}")
                break
            self._stop_event.wait(self._read_interval)

    def _emit_output(self, text: str) -> None:
        """写入输出缓冲并广播 output 事件。"""
        with self._output_lock:
            self._output_seq += 1
            seq = self._output_seq
            self._output_buf.append((seq, text))
        self._emit(EVENT_OUTPUT, {"text": text, "seq": seq})

    # ── 发送 ──────────────────────────────────────────────────
    def send(self, text: str, append_newline: bool = True) -> int:
        """发送文本到 RTT 通道 0。返回写入字节数。未连接抛 RTTError。"""
        if not text:
            return 0
        with self._lock:
            if not self._connected or not self._jlink:
                raise RTTError("not connected")
            payload = text + ("\n" if append_newline else "")
            send_data = payload.encode(self._charset, errors="replace")
            total = len(send_data)
            data = list(send_data)
            offset = 0
            retries = 0
            try:
                with self._jlink_io_lock:
                    if not self._connected or not self._jlink:
                        raise RTTError("not connected")
                    while offset < total:
                        written = self._jlink.rtt_write(0, data[offset:])
                        offset += written
                        if written == 0:
                            if retries < self._send_retries:
                                retries += 1
                                time.sleep(0.01)
                            else:
                                break
                        else:
                            retries = 0

                # 回显
                if self._echo_send:
                    if offset < total:
                        self._emit_output(
                            f"[发送 {offset}/{total}B 截断] {text}\n"
                        )
                    else:
                        self._emit_output(f"[发送 {total}B] {text}\n")
                return offset
            except RTTError:
                raise
            except Exception as e:
                msg = f"[发送错误] {e}\n"
                self._emit_output(msg)
                self._emit(EVENT_ERROR, {"message": f"send error: {e}"})
                with self._lock:
                    self._set_jlink_status("异常", f"send error: {e}")
                self._emit_status()
                self._schedule_recovery(f"发送失败: {e}")
                raise RTTError(f"send error: {e}") from e

    def reset_target(self) -> bool:
        """Reset target MCU through the active J-Link session."""
        return self._target_control(
            method_names=("reset",),
            success_text="[系统] 已通过 J-Link 复位目标。\n",
            error_prefix="reset",
            recovery_text="复位失败",
        )

    def halt_target(self) -> bool:
        """Halt target MCU through the active J-Link session."""
        return self._target_control(
            method_names=("halt",),
            success_text="[系统] 已通过 J-Link 暂停目标。\n",
            error_prefix="halt",
            recovery_text="暂停失败",
        )

    def run_target(self) -> bool:
        """Resume target MCU through the active J-Link session."""
        return self._target_control(
            method_names=("go", "restart"),
            success_text="[系统] 已通过 J-Link 运行目标。\n",
            error_prefix="run",
            recovery_text="运行失败",
        )

    def _target_control(
        self,
        method_names: Tuple[str, ...],
        success_text: str,
        error_prefix: str,
        recovery_text: str,
    ) -> bool:
        try:
            with self._jlink_io_lock:
                if not self._connected or not self._jlink:
                    raise RTTError("not connected", kind="not_connected")
                command = None
                for method_name in method_names:
                    candidate = getattr(self._jlink, method_name, None)
                    if callable(candidate):
                        command = candidate
                        break
                if command is None:
                    joined = "/".join(method_names)
                    raise RTTError(
                        f"J-Link command unavailable: {joined}",
                        kind="jlink_error",
                    )
                command()
            self._emit_output(success_text)
            return True
        except RTTError:
            raise
        except Exception as e:
            msg = f"{error_prefix} error: {e}"
            self._emit(EVENT_ERROR, {"message": msg})
            with self._lock:
                self._set_jlink_status("异常", msg)
            self._emit_status()
            self._schedule_recovery(f"{recovery_text}: {e}")
            raise RTTError(msg) from e

    # ── 输出拉取 ──────────────────────────────────────────────
    def get_output(
        self, since: int = 0, limit: int = 10000, clear: bool = False
    ) -> Tuple[List[str], int]:
        """增量拉取输出。返回 (lines, next_cursor)。

        - since: 上次返回的 next_cursor，0 表示从头取当前缓冲。
        - limit: 最多返回多少条（每条对应一次 _emit_output 调用）。
        - clear: 取走后是否从缓冲移除（影响后续 since 行为）。
        """
        with self._output_lock:
            items = [it for it in self._output_buf if it[0] > since]
            if len(items) > limit:
                items = items[-limit:]
            lines = [it[1] for it in items]
            next_cursor = items[-1][0] if items else since
            if clear and items:
                # 仅清除已取走的部分
                last_taken = items[-1][0]
                while self._output_buf and self._output_buf[0][0] <= last_taken:
                    self._output_buf.popleft()
            return lines, next_cursor

    def clear_output(self, announce: bool = True) -> None:
        with self._output_lock:
            self._output_buf.clear()
            # seq 继续递增，避免 since=0 的旧客户端重复取到旧数据语义混乱
        if announce:
            self._emit_output("[系统] 输出已清空。\n")

    # ── 运行态内存读取 / 变量监控 ─────────────────────────────
    def read_memory(self, address: int, size: int) -> bytes:
        """Read target memory without halting through the active J-Link session."""
        address, size = self._normalize_memory_read_args(address, size)
        started = time.monotonic()
        try:
            with self._jlink_io_lock:
                if not self._connected or not self._jlink:
                    raise RTTError("not connected", kind="not_connected")
                data = self._jlink.memory_read8(address, size)
            result = bytes(data)
            if len(result) != size:
                raise RTTError(
                    f"memory read short: {len(result)}/{size}",
                    kind="short_read",
                )
            self._record_memory_read_result(
                ok=True,
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
            return result
        except RTTError as e:
            self._record_memory_read_result(
                ok=False,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=str(e),
                error_kind=e.kind,
            )
            raise
        except Exception as e:
            kind = self._classify_memory_read_error(e)
            message = f"memory read error [{kind}]: {e}"
            self._record_memory_read_result(
                ok=False,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=message,
                error_kind=kind,
            )
            if kind == "connection_lost":
                with self._lock:
                    self._connected = False
                    self._set_jlink_status("异常", message)
                self.stop_memory_watch()
                self._emit_status()
                self._schedule_recovery(f"内存读取失败: {e}")
            raise RTTError(message, kind=kind) from e

    def _normalize_memory_read_args(self, address: Any, size: Any) -> Tuple[int, int]:
        try:
            addr = int(str(address).strip(), 0) if not isinstance(address, int) else address
        except (TypeError, ValueError) as e:
            raise RTTError(f"memory read address invalid: {address}", kind="invalid_params") from e
        try:
            read_size = int(str(size).strip(), 0) if not isinstance(size, int) else size
        except (TypeError, ValueError) as e:
            raise RTTError(f"memory read size invalid: {size}", kind="invalid_params") from e

        if addr < 0:
            raise RTTError("memory read address must be non-negative", kind="invalid_params")
        if read_size <= 0:
            raise RTTError("memory read size must be positive", kind="invalid_params")
        if read_size > self._memory_read_limit:
            raise RTTError(
                f"memory read size exceeds limit: {read_size}/{self._memory_read_limit}",
                kind="invalid_params",
            )
        if addr > MEMORY_READ_ADDRESS_LIMIT or addr + read_size - 1 > MEMORY_READ_ADDRESS_LIMIT:
            raise RTTError("memory read address range overflow", kind="invalid_params")
        return addr, read_size

    @staticmethod
    def _classify_memory_read_error(exc: Exception) -> str:
        text = str(exc).lower()
        disconnected_markers = (
            "not connected",
            "disconnected",
            "connection",
            "no emulator",
            "no probe",
            "target not connected",
        )
        access_markers = (
            "bus fault",
            "fault",
            "access",
            "permission",
            "out of range",
            "invalid address",
        )
        if any(marker in text for marker in disconnected_markers):
            return "connection_lost"
        if any(marker in text for marker in access_markers):
            return "access_error"
        return "jlink_error"

    def _record_memory_read_result(
        self,
        ok: bool,
        latency_ms: float,
        error: str = "",
        error_kind: str = "",
    ) -> None:
        with self._lock:
            self._memory_read_count += 1
            self._last_memory_read_at = time.time()
            self._last_memory_read_latency_ms = round(float(latency_ms), 3)
            if ok:
                self._last_memory_read_error = ""
                self._last_memory_read_error_kind = ""
            else:
                self._memory_read_fail_count += 1
                self._last_memory_read_error = str(error)
                self._last_memory_read_error_kind = str(error_kind or "internal")

    def add_watch_item(
        self,
        name: str,
        address: int,
        value_type: str = "u32",
        period_ms: int = 500,
        enabled: bool = True,
        source: str = "manual",
    ) -> Dict[str, Any]:
        try:
            return self._watch_manager.add_item(
                name=name,
                address=address,
                value_type=value_type,
                period_ms=period_ms,
                enabled=enabled,
                source=source,
            )
        except WatchError as e:
            raise RTTError(str(e)) from e

    def replace_watch_items(self, items: List[Dict[str, Any]]) -> None:
        try:
            self._watch_manager.replace_items(items)
        except WatchError as e:
            raise RTTError(str(e)) from e

    def remove_watch_item(self, item_id: str) -> None:
        self._watch_manager.remove_item(item_id)

    def clear_watch_items(self) -> None:
        self._watch_manager.clear_items()

    def set_watch_item_enabled(self, item_id: str, enabled: bool) -> None:
        self._watch_manager.set_item_enabled(item_id, enabled)

    def list_watch_items(self, include_runtime: bool = True) -> List[Dict[str, Any]]:
        return self._watch_manager.list_items(include_runtime=include_runtime)

    def get_memory_watch_stats(self) -> Dict[str, Any]:
        return self._watch_manager.get_stats()

    def get_memory_watch_budget(self) -> Dict[str, Any]:
        return self._watch_manager.get_budget()

    def set_memory_watch_budget(
        self,
        max_read_calls_per_cycle: Optional[Any] = None,
        max_bytes_per_cycle: Optional[Any] = None,
        max_cycle_ms: Optional[Any] = None,
        merge_gap: Optional[Any] = None,
    ) -> Dict[str, Any]:
        try:
            budget = self._watch_manager.set_budget(
                max_read_calls_per_cycle=max_read_calls_per_cycle,
                max_bytes_per_cycle=max_bytes_per_cycle,
                max_cycle_ms=max_cycle_ms,
                merge_gap=merge_gap,
            )
        except WatchError as e:
            raise RTTError(str(e), kind="invalid_params") from e
        self._emit(EVENT_CONFIG, {"memory_watch_budget": budget})
        return budget

    def start_memory_watch(self) -> bool:
        if not self.is_connected:
            raise RTTError("not connected", kind="not_connected")
        return self._watch_manager.start()

    def stop_memory_watch(self) -> None:
        self._watch_manager.stop()

    def memory_watch_running(self) -> bool:
        return self._watch_manager.is_running

    def load_axf_variables(self, path: str) -> List[Dict[str, Any]]:
        try:
            return load_axf_symbols(path)
        except WatchError as e:
            raise RTTError(str(e)) from e
        except Exception as e:
            raise RTTError(f"AXF 加载失败: {e}") from e

    def _emit_watch_update(self, items: List[Dict[str, Any]]) -> None:
        self._emit(
            EVENT_WATCH,
            {
                "items": items,
                "running": self.memory_watch_running(),
                "stats": self.get_memory_watch_stats(),
            },
        )

    # ── 配置 ──────────────────────────────────────────────────
    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "device": self._device,
                "iface": self._iface,
                "charset": self._charset,
                "echo_send": self._echo_send,
                "hex_dump": self._hex_dump,
                "connected": self._connected,
                "jlink_status": self._jlink_status,
                "jlink_status_detail": self._jlink_status_detail,
                "last_health_check": self._last_health_check,
                "health_check_interval": self._health_check_interval,
                "health_reconnect_attempts": self._health_reconnect_attempts,
                "memory_read_limit": self._memory_read_limit,
                "memory_read_count": self._memory_read_count,
                "memory_read_fail_count": self._memory_read_fail_count,
                "last_memory_read_at": self._last_memory_read_at,
                "last_memory_read_latency_ms": self._last_memory_read_latency_ms,
                "last_memory_read_error": self._last_memory_read_error,
                "last_memory_read_error_kind": self._last_memory_read_error_kind,
                "memory_watch_budget": self.get_memory_watch_budget(),
            }

    def set_config(self, **kwargs: Any) -> Dict[str, Any]:
        """更新运行期配置。支持 device/iface/charset/echo_send/hex_dump。
        已连接时更改 device/iface/charset 不会重连，需显式 disconnect+connect。
        """
        with self._lock:
            for k in ("device", "iface", "charset"):
                if k in kwargs and kwargs[k] is not None:
                    setattr(self, f"_{k}", str(kwargs[k]))
            if "echo_send" in kwargs and kwargs["echo_send"] is not None:
                self._echo_send = bool(kwargs["echo_send"])
            if "hex_dump" in kwargs and kwargs["hex_dump"] is not None:
                self._hex_dump = bool(kwargs["hex_dump"])
        cfg = self.get_config()
        self._emit(EVENT_CONFIG, cfg)
        return cfg

    # ── GUI 配置（设备历史等）持久化 ──────────────────────────
    def load_gui_config(self) -> Dict[str, Any]:
        if os.path.exists(GUI_CONFIG_FILE):
            try:
                with open(GUI_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._gui_history = data
                    # 同步到运行期配置
                    if data.get("last_device"):
                        self._device = data["last_device"]
                    if data.get("last_charset"):
                        self._charset = data["last_charset"]
                    if data.get("echo_send") is not None:
                        self._echo_send = data["echo_send"]
                    if data.get("hex_dump") is not None:
                        self._hex_dump = data["hex_dump"]
                    if isinstance(data.get("watch_budget"), dict):
                        try:
                            self._watch_manager.set_budget(**data["watch_budget"])
                        except WatchError:
                            pass
                    return data
            except Exception:
                pass
        return dict(self._gui_history)

    def save_gui_config(
        self,
        device: Optional[str] = None,
        devices: Optional[List[str]] = None,
        input_history: Optional[List[str]] = None,
        watch_items: Optional[List[Dict[str, Any]]] = None,
        watch_axf_path: Optional[str] = None,
        watch_panel_visible: Optional[bool] = None,
        watch_budget: Optional[Dict[str, Any]] = None,
    ) -> None:
        """保存 GUI 侧配置。device/devices/input_history 由 GUI 提供。"""
        with self._lock:
            cur_device = (device or self._device).strip()
            if not cur_device:
                return
            devs = list(self._gui_history.get("devices", []))
            if cur_device not in devs:
                devs.append(cur_device)
            self._gui_history["devices"] = devs
            self._gui_history["last_device"] = cur_device
            self._gui_history["last_charset"] = self._charset
            self._gui_history["echo_send"] = self._echo_send
            self._gui_history["hex_dump"] = self._hex_dump
            if devices is not None:
                self._gui_history["devices"] = devices
            if input_history is not None:
                self._gui_history["input_history"] = input_history
            if watch_items is not None:
                self._gui_history["watch_items"] = watch_items
            if watch_axf_path is not None:
                self._gui_history["watch_axf_path"] = watch_axf_path
            if watch_panel_visible is not None:
                self._gui_history["watch_panel_visible"] = bool(watch_panel_visible)
            if watch_budget is not None:
                self._gui_history["watch_budget"] = dict(watch_budget)
            data = dict(self._gui_history)
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(GUI_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def get_gui_devices(self) -> List[str]:
        return list(self._gui_history.get("devices", []))

    # ── Agent 配置持久化（独立文件） ──────────────────────────
    def load_agent_config(self) -> Dict[str, Any]:
        if os.path.exists(AGENT_CONFIG_FILE):
            try:
                with open(AGENT_CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_agent_config(self, config: Dict[str, Any]) -> None:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(AGENT_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    # ── Agent 命令历史（独立文件） ────────────────────────────
    def load_agent_history(self) -> List[str]:
        if os.path.exists(AGENT_HISTORY_FILE):
            try:
                with open(AGENT_HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._agent_history = [str(x) for x in data]
                        return list(self._agent_history)
            except Exception:
                pass
        return []

    def save_agent_history(self) -> None:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(AGENT_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self._agent_history, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def append_agent_history(self, command: str) -> None:
        """记录一条 agent 命令（去重相邻、限长）。command 通常为 'method: 摘要'。"""
        if not command:
            return
        with self._lock:
            if not self._agent_history or self._agent_history[-1] != command:
                self._agent_history.append(command)
                if len(self._agent_history) > AGENT_HISTORY_LIMIT:
                    self._agent_history.pop(0)

    def get_agent_history(self, limit: int = 100) -> List[str]:
        with self._lock:
            return list(self._agent_history[-limit:])

    def clear_agent_history(self) -> None:
        with self._lock:
            self._agent_history.clear()
        self.save_agent_history()

    # ── 关闭 ──────────────────────────────────────────────────
    def close(self) -> None:
        """彻底关闭：断开连接 + 清空订阅者。"""
        try:
            self.stop_memory_watch()
            self.disconnect()
        except Exception:
            pass
        with self._sub_lock:
            self._subs.clear()
