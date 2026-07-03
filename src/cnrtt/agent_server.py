"""cnrtt AI agent 服务端 —— JSON-RPC 2.0 over TCP。

传输协议：
- 每条消息前 4 字节大端无符号整数表示 JSON 长度（framing），便于流式解析。
- 单连接双向：client 发请求/响应，server 主动推送 notify（无 id）。

鉴权（可选）：
- 启动时传入 token；client 每个请求顶层带 {"auth": "<token>"}，
  不匹配返回错误码 -32002。

推送策略：
- 订阅 core 的 output 事件，攒批 50ms 后合并为一行推送（高频 MCU 输出
  不会刷死 agent）。

线程模型：
- 每个连接一个线程处理读取/分发；推送在连接线程内发送。
- core 回调线程安全，server 仅做转发。
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any, Dict, Optional

from cnrtt.core import (
    EVENT_ERROR,
    EVENT_OUTPUT,
    EVENT_STATUS,
    RTTCore,
    RTTError,
)

# JSON-RPC 错误码
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_NOT_CONNECTED = -32001
ERR_AUTH_FAILED = -32002

# 攒批推送间隔（秒）
PUSH_BATCH_INTERVAL = 0.05
# 读取缓冲
RECV_CHUNK = 4096
# 消息最大长度（防恶意大包）
MAX_MSG_LEN = 8 * 1024 * 1024


class AgentServer:
    """JSON-RPC 2.0 over TCP 服务端，绑 127.0.0.1。"""

    def __init__(
        self,
        core: RTTCore,
        host: str = "127.0.0.1",
        port: int = 7000,
        token: Optional[str] = None,
    ) -> None:
        self.core = core
        self.host = host
        self.port = port
        self.token = token

        self._server_sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._running = False
        self._clients_lock = threading.Lock()
        self._clients: Dict[int, "_ClientHandler"] = {}
        self._next_client_id = 1

    # ── 生命周期 ──────────────────────────────────────────────
    def start(self) -> None:
        """启动服务（非阻塞）：监听端口并开启接受连接线程。"""
        if self._running:
            return
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(8)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="cnrtt-agent-accept"
        )
        self._accept_thread.start()

    def stop(self) -> None:
        """停止服务：关闭所有连接与监听 socket。"""
        self._running = False
        if self._server_sock:
            try:
                # 解除 accept 阻塞
                try:
                    self._server_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        with self._clients_lock:
            clients = list(self._clients.values())
        for c in clients:
            c.close()
        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=1.0)
        self._accept_thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    def serve_forever(self) -> None:
        """阻塞当前线程直到 stop()。供 headless 入口使用。"""
        try:
            while self._running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.stop()

    # ── 接受连接 ──────────────────────────────────────────────
    def _accept_loop(self) -> None:
        while self._running and self._server_sock:
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                # 监听 socket 关闭
                break
            with self._clients_lock:
                cid = self._next_client_id
                self._next_client_id += 1
            handler = _ClientHandler(self, cid, conn, addr)
            with self._clients_lock:
                self._clients[cid] = handler
            handler.start()

    def _remove_client(self, cid: int) -> None:
        with self._clients_lock:
            self._clients.pop(cid, None)


# ── 单连接处理 ────────────────────────────────────────────────
class _ClientHandler:
    """每个 agent 连接的处理者：读请求、分发方法、推送事件。"""

    def __init__(
        self, server: AgentServer, cid: int, conn: socket.socket, addr
    ) -> None:
        self.server = server
        self.cid = cid
        self.conn = conn
        self.addr = addr
        self.core: RTTCore = server.core

        self._recv_buf = bytearray()
        self._thread: Optional[threading.Thread] = None
        self._alive = False

        # 攒批输出
        self._batch_lock = threading.Lock()
        self._batch_buf: list = []
        self._batch_timer: Optional[threading.Timer] = None
        self._sub_id: Optional[int] = None

    def start(self) -> None:
        self._alive = True
        # 订阅 core 事件
        self._sub_id = self.core.subscribe(self._on_core_event)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"cnrtt-agent-c{self.cid}"
        )
        self._thread.start()

    def close(self) -> None:
        self._alive = False
        if self._sub_id is not None:
            try:
                self.core.unsubscribe(self._sub_id)
            except Exception:
                pass
            self._sub_id = None
        self._flush_batch(force=True)
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    # ── core 事件 → 攒批 / 直推 ───────────────────────────────
    def _on_core_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if not self._alive:
            return
        if event_type == EVENT_OUTPUT:
            # 攒批
            with self._batch_lock:
                self._batch_buf.append(payload.get("text", ""))
                if self._batch_timer is None:
                    self._batch_timer = threading.Timer(
                        PUSH_BATCH_INTERVAL, self._flush_batch
                    )
                    self._batch_timer.daemon = True
                    self._batch_timer.start()
        elif event_type == EVENT_STATUS:
            self._send_notify("status", {"connected": payload.get("connected", False)})
        elif event_type == EVENT_ERROR:
            self._send_notify("error", {"message": payload.get("message", "")})

    def _flush_batch(self, force: bool = False) -> None:
        with self._batch_lock:
            if self._batch_timer is not None:
                self._batch_timer.cancel()
                self._batch_timer = None
            if not self._batch_buf:
                return
            text = "".join(self._batch_buf)
            self._batch_buf.clear()
        if text:
            self._send_notify("output", {"text": text})

    # ── 主循环：读 framing → JSON → 分发 ──────────────────────
    def _run(self) -> None:
        try:
            while self._alive:
                msg = self._read_message()
                if msg is None:
                    break
                self._handle_message(msg)
        except Exception:
            pass
        finally:
            self._flush_batch(force=True)
            self.server._remove_client(self.cid)

    def _read_message(self) -> Optional[dict]:
        """读取一条 framing 消息并解析为 dict。连接关闭返回 None。"""
        # 读 4 字节长度
        while len(self._recv_buf) < 4:
            chunk = self.conn.recv(RECV_CHUNK)
            if not chunk:
                return None
            self._recv_buf.extend(chunk)
        (length,) = struct.unpack(">I", self._recv_buf[:4])
        if length <= 0 or length > MAX_MSG_LEN:
            self._send_error(None, ERR_INVALID_REQUEST, "invalid message length")
            return None
        # 读 JSON 体
        while len(self._recv_buf) < 4 + length:
            chunk = self.conn.recv(RECV_CHUNK)
            if not chunk:
                return None
            self._recv_buf.extend(chunk)
        body = self._recv_buf[4 : 4 + length]
        del self._recv_buf[: 4 + length]
        try:
            return json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_error(None, ERR_PARSE_ERROR, f"json parse error: {e}")
            return None

    def _handle_message(self, msg: dict) -> None:
        # 基本校验
        if not isinstance(msg, dict):
            self._send_error(None, ERR_INVALID_REQUEST, "request must be object")
            return
        # 鉴权
        if self.server.token is not None:
            if msg.get("auth") != self.server.token:
                self._send_error(
                    msg.get("id"), ERR_AUTH_FAILED, "authentication failed"
                )
                return
        method = msg.get("method")
        params = msg.get("params") or {}
        msg_id = msg.get("id")
        if not isinstance(method, str):
            if msg_id is not None:
                self._send_error(msg_id, ERR_INVALID_REQUEST, "missing method")
            return
        try:
            result = self._dispatch(method, params)
            if msg_id is not None:
                self._send_response(msg_id, result)
        except RTTError as e:
            if msg_id is not None:
                self._send_error(msg_id, ERR_NOT_CONNECTED if "not connected" in str(e).lower() else ERR_INTERNAL, str(e))
        except _RpcError as e:
            if msg_id is not None:
                self._send_error(msg_id, e.code, e.message)
        except Exception as e:
            if msg_id is not None:
                self._send_error(msg_id, ERR_INTERNAL, f"internal error: {e}")

    # ── 方法分发 ──────────────────────────────────────────────
    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "status":
            return self.core.get_config()
        if method == "connect":
            device = params.get("device")
            iface = params.get("iface")
            charset = params.get("charset")
            if not device:
                # 用 core 当前配置
                cfg = self.core.get_config()
                device = cfg["device"]
                iface = iface or cfg["iface"]
                charset = charset or cfg["charset"]
            self.core.connect(device=device, iface=iface, charset=charset)
            self.core.append_agent_history(f"connect: {device}/{iface}")
            self.core.save_agent_history()
            return {"connected": True}
        if method == "disconnect":
            self.core.disconnect()
            self.core.append_agent_history("disconnect")
            self.core.save_agent_history()
            return {"connected": False}
        if method == "send":
            text = params.get("text")
            if text is None or not isinstance(text, str):
                raise _RpcError(ERR_INVALID_PARAMS, "text required")
            append_nl = params.get("append_newline", True)
            n = self.core.send(text, append_newline=append_nl)
            self.core.append_agent_history(f"send: {text[:80]}")
            self.core.save_agent_history()
            return {"bytes_sent": n}
        if method == "get_output":
            since = int(params.get("since", 0))
            limit = int(params.get("limit", 10000))
            clear = bool(params.get("clear", False))
            lines, cursor = self.core.get_output(since=since, limit=limit, clear=clear)
            return {"lines": lines, "next_cursor": cursor}
        if method == "clear_output":
            self.core.clear_output()
            return {"ok": True}
        if method == "get_config":
            return self.core.get_config()
        if method == "set_config":
            return self.core.set_config(**params)
        if method == "save_config":
            self.core.save_gui_config()
            return {"ok": True}
        if method == "get_agent_history":
            return {"history": self.core.get_agent_history(int(params.get("limit", 100)))}
        if method == "clear_agent_history":
            self.core.clear_agent_history()
            return {"ok": True}
        raise _RpcError(ERR_METHOD_NOT_FOUND, f"method not found: {method}")

    # ── 发送 ──────────────────────────────────────────────────
    def _send_json(self, obj: dict) -> None:
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            frame = struct.pack(">I", len(body)) + body
            self.conn.sendall(frame)
        except Exception:
            self._alive = False

    def _send_response(self, msg_id: Any, result: Any) -> None:
        self._send_json({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _send_error(self, msg_id: Any, code: int, message: str) -> None:
        self._send_json(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        )

    def _send_notify(self, method: str, params: dict) -> None:
        # 通知无 id
        self._send_json({"jsonrpc": "2.0", "method": method, "params": params})


class _RpcError(Exception):
    """内部用于携带 JSON-RPC 错误码的异常。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
