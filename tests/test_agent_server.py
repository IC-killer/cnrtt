"""AgentServer 集成测试 —— 起真实 TCP server（mock core），用 AgentClient 走全协议。"""

import socket
import threading
import time
from unittest import mock

import pytest

from cnrtt.agent_server import (
    AgentServer,
    ERR_AUTH_FAILED,
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
)
from cnrtt.agent_client import AgentClient, AgentError
from cnrtt.core import RTTCore, RTTError


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server_factory(tmp_path, monkeypatch):
    """返回一个工厂：创建绑定了 mock core 的 server。"""
    # 重定向配置目录
    d = tmp_path / ".cnrtt"
    monkeypatch.setattr("cnrtt.core.CONFIG_DIR", str(d))
    monkeypatch.setattr("cnrtt.core.GUI_CONFIG_FILE", str(d / "rtt_history.json"))
    monkeypatch.setattr("cnrtt.core.AGENT_CONFIG_FILE", str(d / "agent_config.json"))
    monkeypatch.setattr("cnrtt.core.AGENT_HISTORY_FILE", str(d / "agent_history.json"))

    created = []

    def _make(token=None):
        core = RTTCore(read_interval=0.005)
        port = _free_port()
        srv = AgentServer(core, host="127.0.0.1", port=port, token=token)
        srv.start()
        created.append(srv)
        return srv, core, port

    yield _make

    for s in created:
        s.stop()


def _wait_server_ready(port, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server not ready")


# ── 基本协议 ────────────────────────────────────────────────
def test_status_returns_config(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        r = c.call("status")
        assert r["device"] == "STM32F407VE"
        assert r["connected"] is False
    finally:
        c.close()


def test_set_config_and_get_config(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        r = c.call("set_config", {"device": "STM32H743", "echo_send": True})
        assert r["device"] == "STM32H743"
        assert r["echo_send"] is True
        r2 = c.call("get_config")
        assert r2["device"] == "STM32H743"
    finally:
        c.close()


def test_method_not_found(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        with pytest.raises(AgentError) as ei:
            c.call("nonexistent_method")
        assert ei.value.code == ERR_METHOD_NOT_FOUND
    finally:
        c.close()


def test_get_output_returns_buffer(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        # core 直接触发输出（绕过连接）
        core._emit_output("line1\n")
        core._emit_output("line2\n")
        r = c.call("get_output")
        assert "line1\n" in r["lines"]
        assert "line2\n" in r["lines"]
        assert r["next_cursor"] > 0
    finally:
        c.close()


def test_clear_output(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        core._emit_output("data1")
        c.call("clear_output")
        r = c.call("get_output")
        # clear_output 自身会发一条 [系统] 提示
        assert all("data1" not in l for l in r["lines"])
    finally:
        c.close()


# ── agent 命令历史 ──────────────────────────────────────────
def test_agent_history_via_rpc(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        core.append_agent_history("manual: test")
        core.save_agent_history()
        r = c.call("get_agent_history", {"limit": 100})
        assert "manual: test" in r["history"]
        c.call("clear_agent_history")
        r2 = c.call("get_agent_history", {"limit": 100})
        assert r2["history"] == []
    finally:
        c.close()


# ── connect/disconnect（mock pylink） ───────────────────────
def _patch_pylink():
    jlink = mock.MagicMock()
    jlink.connected.return_value = True
    jlink.target_connected.return_value = True
    jlink.rtt_read.return_value = []
    jlink.rtt_write.side_effect = lambda ch, data: len(data)
    jlink.memory_read8.return_value = [0x78, 0x56, 0x34, 0x12]
    pl = mock.MagicMock()
    pl.JLink.return_value = jlink
    pl.enums.JLinkInterfaces.SWD = "SWD"
    pl.enums.JLinkInterfaces.JTAG = "JTAG"
    return mock.patch("cnrtt.core.pylink", pl), jlink


def test_connect_and_send_and_disconnect(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    patcher, jlink = _patch_pylink()
    patcher.start()
    try:
        r = c.call("connect", {"device": "STM32F407VE"})
        assert r["connected"] is True
        r2 = c.call("send", {"text": "led on"})
        assert r2["bytes_sent"] == 7
        r3 = c.call("disconnect")
        assert r3["connected"] is False
    finally:
        patcher.stop()
        c.close()


def test_reset_and_read_memory_rpc(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    patcher, jlink = _patch_pylink()
    patcher.start()
    try:
        c.call("connect", {"device": "STM32F407VE"})
        reset = c.call("reset")
        assert reset == {"ok": True}
        jlink.reset.assert_called_once()

        result = c.call("read_memory", {"address": "0x20000000", "size": "4"})
        assert result == {
            "address": 0x20000000,
            "size": 4,
            "hex": "78 56 34 12",
            "bytes": [0x78, 0x56, 0x34, 0x12],
        }
        jlink.memory_read8.assert_called_with(0x20000000, 4)
    finally:
        try:
            c.call("disconnect")
        except Exception:
            pass
        patcher.stop()
        c.close()


def test_read_memory_invalid_params_rpc(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        with pytest.raises(AgentError) as err:
            c.call("read_memory", {"address": "bad", "size": 4})
        assert err.value.code == ERR_INVALID_PARAMS
    finally:
        c.close()


def test_watch_control_rpc(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    patcher, jlink = _patch_pylink()
    patcher.start()
    try:
        added = c.call(
            "watch_add",
            {
                "name": "counter",
                "address": "0x20000000",
                "type": "u32",
                "period_ms": 50,
            },
        )
        assert added["name"] == "counter"
        assert added["source"] == "agent"

        listing = c.call("watch_list")
        assert listing["running"] is False
        assert listing["items"][0]["name"] == "counter"
        assert "stats" in listing

        c.call("watch_enable", {"id": added["id"], "enabled": False})
        assert c.call("watch_list")["items"][0]["enabled"] is False
        c.call("watch_enable", {"id": added["id"], "enabled": True})

        c.call("connect", {"device": "STM32F407VE"})
        assert c.call("watch_start") == {"running": True}
        deadline = time.monotonic() + 1.0
        latest = {}
        while time.monotonic() < deadline:
            latest = c.call("watch_list")
            if latest["items"][0].get("read_count", 0) >= 1:
                break
            time.sleep(0.02)
        assert latest["items"][0]["value"] == "305419896 (0x12345678)"
        assert latest["stats"]["read_calls"] >= 1
        assert c.call("watch_stop") == {"running": False}

        assert c.call("watch_stats")["read_calls"] >= 1
        assert c.call("watch_remove", {"id": added["id"]}) == {"ok": True}
        assert c.call("watch_clear") == {"ok": True}
    finally:
        try:
            c.call("disconnect")
        except Exception:
            pass
        patcher.stop()
        c.close()


def test_send_without_connect_returns_error(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c = AgentClient("127.0.0.1", port)
    try:
        with pytest.raises(AgentError):
            c.call("send", {"text": "x"})
    finally:
        c.close()


# ── 鉴权 ────────────────────────────────────────────────────
def test_token_auth_required(server_factory):
    srv, core, port = server_factory(token="secret123")
    _wait_server_ready(port)
    # 不带 token
    c = AgentClient("127.0.0.1", port)
    try:
        with pytest.raises(AgentError) as ei:
            c.call("status")
        assert ei.value.code == ERR_AUTH_FAILED
    finally:
        c.close()

    # 带正确 token
    c2 = AgentClient("127.0.0.1", port, token="secret123")
    try:
        r = c2.call("status")
        assert r["device"] == "STM32F407VE"
    finally:
        c2.close()


# ── 推送（notify） ──────────────────────────────────────────
def test_output_push_and_status_push(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    # 注意：AgentClient 的 call() 与 watch() 共用同一 socket/缓冲，
    # 并发读会互相抢消息。因此用两个独立 client：一个 watch，一个 call。
    # 另需第三个 client 做一次握手，确保 server 端已为 watch client 建立订阅。
    c_probe = AgentClient("127.0.0.1", port)
    c_watch = AgentClient("127.0.0.1", port)
    c_call = AgentClient("127.0.0.1", port)
    outputs = []
    statuses = []
    try:
        # 1) 先让 watch client 建立 TCP 连接，并等 server 端 handler 完成订阅
        c_watch.connect()
        time.sleep(0.2)  # 等 server 端 _ClientHandler.start() → core.subscribe 完成

        # 2) 启动 watch 监听
        stop = threading.Event()
        t = threading.Thread(
            target=c_watch.watch,
            kwargs={
                "on_output": lambda txt: outputs.append(txt),
                "on_status": lambda conn: statuses.append(conn),
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()

        # 3) 触发输出事件（此时 server 端已订阅，必定能收到）
        core._emit_output("pushed-line\n")

        # 4) 触发 status 事件
        patcher, jlink = _patch_pylink()
        patcher.start()
        try:
            c_call.call("connect", {"device": "STM32F407VE"})
        finally:
            patcher.stop()

        # 5) 等推送（攒批 50ms + 网络往返）
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not outputs:
            time.sleep(0.02)
        stop.set()
        t.join(timeout=1.0)
        assert any("pushed-line" in o for o in outputs), f"outputs={outputs}"
        assert True in statuses, f"statuses={statuses}"
    finally:
        c_watch.close()
        c_call.close()
        c_probe.close()


def test_watch_push(server_factory):
    srv, core, port = server_factory()
    _wait_server_ready(port)
    c_watch = AgentClient("127.0.0.1", port)
    pushes = []
    try:
        c_watch.connect()
        time.sleep(0.2)

        stop = threading.Event()
        t = threading.Thread(
            target=c_watch.watch,
            kwargs={
                "on_watch": lambda payload: pushes.append(payload),
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()

        core.add_watch_item("counter", 0x20000000, "u32", period_ms=50)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not pushes:
            time.sleep(0.02)
        stop.set()
        t.join(timeout=1.0)

        assert pushes
        assert pushes[-1]["items"][0]["name"] == "counter"
        assert pushes[-1]["running"] is False
        assert "stats" in pushes[-1]
    finally:
        c_watch.close()
