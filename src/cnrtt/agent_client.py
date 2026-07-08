"""cnrtt AI agent 参考客户端 + CLI。

提供两个用途：
1. AgentClient 类 —— AI agent 集成时直接 copy 使用（纯 stdlib）。
2. CLI 入口 —— 人工调试协议：status / connect / disconnect / send /
   get_output / clear / config / reset / read_memory / watch 控制 / history。

传输协议与 agent_server.py 一致：4 字节大端长度前缀 + JSON-RPC 2.0。
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import threading
import time
from typing import Any, Dict, Optional


class AgentError(Exception):
    """服务端返回的 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class AgentClient:
    """同步 JSON-RPC 客户端，支持请求-响应与服务端 notify 推送。

    用法：
        c = AgentClient("127.0.0.1", 7000)
        c.connect()                     # 底层 TCP 连接
        print(c.call("status"))
        c.call("connect", {"device": "STM32F407VE"})
        c.watch(on_output=lambda t: print(t, end=""))

    线程安全约束：
        call() 与 watch() 共用同一 socket 与接收缓冲，**不可在 watch 运行期间
        对同一 client 调用 call()**，否则两者会互相抢占消息。
        如需在监听推送的同时发请求，请用两个独立 AgentClient 实例
        （一个 watch，一个 call）。
    """

    RECV_CHUNK = 4096
    MAX_MSG_LEN = 8 * 1024 * 1024

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7000,
        token: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._recv_buf = bytearray()
        self._id = 0
        self._lock = threading.Lock()
        self._connected = False

    # ── 连接管理 ──────────────────────────────────────────────
    def connect(self) -> None:
        """建立 TCP 连接（与 RTT 的 connect 方法区分）。"""
        if self._connected:
            return
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        self._sock.settimeout(None)  # 阻塞读
        self._connected = True

    def close(self) -> None:
        self._connected = False
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._recv_buf.clear()

    # ── 请求-响应 ─────────────────────────────────────────────
    def call(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> Any:
        """同步调用一个方法并返回 result。服务端返回 error 抛 AgentError。"""
        with self._lock:
            if not self._connected:
                self.connect()
            self._id += 1
            req_id = self._id
            req: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params:
                req["params"] = params
            if self.token:
                req["auth"] = self.token
            self._send_json(req)
            # 读直到拿到对应 id 的响应（期间收到的 notify 交给回调）
            return self._read_until_response(req_id, timeout)

    # ── 推送监听 ──────────────────────────────────────────────
    def watch(
        self,
        on_output: Optional[Any] = None,
        on_status: Optional[Any] = None,
        on_error: Optional[Any] = None,
        on_watch: Optional[Any] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """持续监听服务端 notify，直到 stop_event 被设置或连接断开。"""
        stop = stop_event or threading.Event()
        while not stop.is_set() and self._connected:
            try:
                msg = self._read_message(timeout=0.5)
            except socket.timeout:
                continue
            except OSError:
                break
            if msg is None:
                break
            if "method" in msg and "id" not in msg:
                method = msg.get("method")
                params = msg.get("params", {})
                if method == "output" and on_output:
                    on_output(params.get("text", ""))
                elif method == "status" and on_status:
                    on_status(params.get("connected"))
                elif method == "error" and on_error:
                    on_error(params.get("message", ""))
                elif method == "watch" and on_watch:
                    on_watch(params)

    # ── 底层读写 ──────────────────────────────────────────────
    def _send_json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._sock.sendall(struct.pack(">I", len(body)) + body)

    def _read_message(self, timeout: Optional[float] = None) -> Optional[dict]:
        old_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            # 4 字节长度
            while len(self._recv_buf) < 4:
                chunk = self._sock.recv(self.RECV_CHUNK)
                if not chunk:
                    return None
                self._recv_buf.extend(chunk)
            (length,) = struct.unpack(">I", self._recv_buf[:4])
            if length <= 0 or length > self.MAX_MSG_LEN:
                raise AgentError(-32600, "invalid message length")
            while len(self._recv_buf) < 4 + length:
                chunk = self._sock.recv(self.RECV_CHUNK)
                if not chunk:
                    return None
                self._recv_buf.extend(chunk)
            body = self._recv_buf[4 : 4 + length]
            del self._recv_buf[: 4 + length]
            return json.loads(body.decode("utf-8"))
        finally:
            if timeout is not None:
                self._sock.settimeout(old_timeout)

    def _read_until_response(self, req_id: int, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError(-32603, "response timeout")
            msg = self._read_message(timeout=remaining)
            if msg is None:
                raise AgentError(-32603, "connection closed")
            if "id" in msg and msg["id"] == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise AgentError(err.get("code", -32603), err.get("message", ""))
                return msg.get("result")
            # 其它消息（notify）此处忽略，watch 模式单独处理


# ── CLI ───────────────────────────────────────────────────────
def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _parse_cli_int(value: str) -> int:
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"非法整数: {value}") from e


def cli_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cnrtt-agent-client",
        description="cnrtt agent 协议调试 CLI",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--token", default=None, help="鉴权 token（若服务端启用）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查询当前状态")
    sub.add_parser("disconnect", help="断开 RTT")
    sub.add_parser("reset", help="通过 J-Link 复位目标")

    p_conn = sub.add_parser("connect", help="连接 RTT")
    p_conn.add_argument("--device", default=None)
    p_conn.add_argument("--iface", default=None, choices=["SWD", "JTAG"])
    p_conn.add_argument("--charset", default=None, choices=["UTF-8", "GB2312"])

    p_send = sub.add_parser("send", help="发送文本")
    p_send.add_argument("--text", required=True)
    p_send.add_argument("--no-newline", action="store_true", help="不追加换行")

    p_get = sub.add_parser("get_output", help="拉取输出")
    p_get.add_argument("--since", type=int, default=0)
    p_get.add_argument("--limit", type=int, default=10000)
    p_get.add_argument("--clear", action="store_true")

    sub.add_parser("clear", help="清空输出缓冲")

    p_mem = sub.add_parser("read_memory", help="读取目标内存")
    p_mem.add_argument("--address", required=True, help="地址，如 0x20000000")
    p_mem.add_argument(
        "--size",
        required=True,
        type=_parse_cli_int,
        help="读取字节数，如 4 或 0x10",
    )

    p_watch_list = sub.add_parser("watch_list", help="列出变量监控项")
    p_watch_list.add_argument(
        "--no-runtime",
        action="store_true",
        help="不返回当前值/错误/计数等运行态字段",
    )

    p_watch_add = sub.add_parser("watch_add", help="添加变量监控项")
    p_watch_add.add_argument("--name", required=True)
    p_watch_add.add_argument("--address", required=True)
    p_watch_add.add_argument("--type", default="u32")
    p_watch_add.add_argument("--period-ms", type=int, default=500)
    p_watch_add.add_argument("--disabled", action="store_true")
    p_watch_add.add_argument("--source", default="agent")

    p_watch_remove = sub.add_parser("watch_remove", help="移除变量监控项")
    p_watch_remove.add_argument("--id", required=True)

    sub.add_parser("watch_clear", help="清空变量监控项")

    p_watch_enable = sub.add_parser("watch_enable", help="启用或禁用变量监控项")
    p_watch_enable.add_argument("--id", required=True)
    state = p_watch_enable.add_mutually_exclusive_group()
    state.add_argument("--enabled", action="store_true")
    state.add_argument("--disabled", action="store_true")

    sub.add_parser("watch_start", help="开始变量采样")
    sub.add_parser("watch_stop", help="停止变量采样")
    sub.add_parser("watch_stats", help="查看最近一轮变量采样统计")

    p_cfg = sub.add_parser("config", help="查询或设置配置")
    p_cfg.add_argument("--set", nargs="*", default=None, help="key=value 形式设置")
    p_cfg.add_argument("--save", action="store_true", help="持久化到配置文件")

    sub.add_parser("history", help="查询 agent 命令历史")
    p_hist = sub.add_parser("clear_history", help="清空 agent 命令历史")

    p_watch = sub.add_parser("watch", help="持续监听服务端推送")
    p_watch.add_argument("--raw", action="store_true", help="原始 JSON 输出")

    args = parser.parse_args(argv)
    client = AgentClient(args.host, args.port, token=args.token)

    try:
        if args.command == "status":
            _print_json(client.call("status"))
        elif args.command == "connect":
            params = {}
            if args.device:
                params["device"] = args.device
            if args.iface:
                params["iface"] = args.iface
            if args.charset:
                params["charset"] = args.charset
            _print_json(client.call("connect", params))
        elif args.command == "disconnect":
            _print_json(client.call("disconnect"))
        elif args.command == "reset":
            _print_json(client.call("reset"))
        elif args.command == "send":
            _print_json(
                client.call(
                    "send",
                    {"text": args.text, "append_newline": not args.no_newline},
                )
            )
        elif args.command == "get_output":
            _print_json(
                client.call(
                    "get_output",
                    {"since": args.since, "limit": args.limit, "clear": args.clear},
                )
            )
        elif args.command == "clear":
            _print_json(client.call("clear_output"))
        elif args.command == "read_memory":
            _print_json(
                client.call(
                    "read_memory",
                    {
                        "address": args.address,
                        "size": args.size,
                    },
                )
            )
        elif args.command == "watch_list":
            _print_json(
                client.call(
                    "watch_list",
                    {"include_runtime": not args.no_runtime},
                )
            )
        elif args.command == "watch_add":
            _print_json(
                client.call(
                    "watch_add",
                    {
                        "name": args.name,
                        "address": args.address,
                        "type": args.type,
                        "period_ms": args.period_ms,
                        "enabled": not args.disabled,
                        "source": args.source,
                    },
                )
            )
        elif args.command == "watch_remove":
            _print_json(client.call("watch_remove", {"id": args.id}))
        elif args.command == "watch_clear":
            _print_json(client.call("watch_clear"))
        elif args.command == "watch_enable":
            _print_json(
                client.call(
                    "watch_enable",
                    {"id": args.id, "enabled": not args.disabled},
                )
            )
        elif args.command == "watch_start":
            _print_json(client.call("watch_start"))
        elif args.command == "watch_stop":
            _print_json(client.call("watch_stop"))
        elif args.command == "watch_stats":
            _print_json(client.call("watch_stats"))
        elif args.command == "config":
            if args.set:
                params = {}
                for kv in args.set:
                    if "=" not in kv:
                        print(f"非法参数: {kv}（应为 key=value）", file=sys.stderr)
                        return 2
                    k, v = kv.split("=", 1)
                    if v.lower() in ("true", "false"):
                        params[k] = v.lower() == "true"
                    else:
                        params[k] = v
                _print_json(client.call("set_config", params))
            else:
                _print_json(client.call("get_config"))
            if args.save:
                _print_json(client.call("save_config"))
        elif args.command == "history":
            _print_json(client.call("get_agent_history", {"limit": 100}))
        elif args.command == "clear_history":
            _print_json(client.call("clear_agent_history"))
        elif args.command == "watch":
            stop = threading.Event()

            def on_output(text):
                if args.raw:
                    print(json.dumps({"output": text}, ensure_ascii=False), flush=True)
                else:
                    print(text, end="", flush=True)

            def on_status(connected):
                print(
                    json.dumps({"status": {"connected": connected}}, ensure_ascii=False),
                    flush=True,
                )

            def on_error(message):
                print(
                    json.dumps({"error": {"message": message}}, ensure_ascii=False),
                    flush=True,
                )

            def on_watch(payload):
                print(
                    json.dumps({"watch": payload}, ensure_ascii=False),
                    flush=True,
                )

            try:
                client.watch(
                    on_output=on_output,
                    on_status=on_status,
                    on_error=on_error,
                    on_watch=on_watch,
                    stop_event=stop,
                )
            except KeyboardInterrupt:
                stop.set()
        return 0
    except AgentError as e:
        print(f"RPC 错误: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(cli_main())
