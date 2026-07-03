"""RTTCore 单元测试 —— mock pylink，验证连接/收发/事件总线/配置/缓冲。"""

import json
import os
import tempfile
import threading
import time
from unittest import mock

import pytest

from cnrtt import core as core_mod
from cnrtt.core import RTTCore, RTTError, EVENT_OUTPUT, EVENT_STATUS, EVENT_CONFIG


@pytest.fixture
def tmp_config_dir(monkeypatch, tmp_path):
    """把配置目录重定向到临时目录，避免污染用户家目录。"""
    d = tmp_path / ".cnrtt"
    monkeypatch.setattr(core_mod, "CONFIG_DIR", str(d))
    monkeypatch.setattr(core_mod, "GUI_CONFIG_FILE", str(d / "rtt_history.json"))
    monkeypatch.setattr(core_mod, "AGENT_CONFIG_FILE", str(d / "agent_config.json"))
    monkeypatch.setattr(core_mod, "AGENT_HISTORY_FILE", str(d / "agent_history.json"))
    return d


@pytest.fixture
def fake_jlink():
    """构造一个可编程的 fake JLink。"""
    jlink = mock.MagicMock()
    jlink.connected.return_value = True
    # rtt_read 默认返回空，测试中可改 side_effect
    jlink.rtt_read.return_value = []
    # rtt_write 返回写入字节数
    jlink.rtt_write.side_effect = lambda channel, data: len(data)
    return jlink


@pytest.fixture
def core(tmp_config_dir, fake_jlink):
    """构造一个 core，connect 时注入 fake_jlink。"""
    c = RTTCore(read_interval=0.005)
    # 让 pylink.JLink() 返回我们的 fake
    with mock.patch.object(core_mod, "pylink") as pl:
        pl.JLink.return_value = fake_jlink
        pl.enums.JLinkInterfaces.SWD = "SWD"
        pl.enums.JLinkInterfaces.JTAG = "JTAG"
        # 暴露给测试用：connect 时使用 patched pylink
        c._patched_pylink = pl
    return c


# ── 配置 ────────────────────────────────────────────────────
def test_default_config(core):
    cfg = core.get_config()
    assert cfg["device"] == "STM32F407VE"
    assert cfg["iface"] == "SWD"
    assert cfg["charset"] == "UTF-8"
    assert cfg["connected"] is False
    assert cfg["echo_send"] is False
    assert cfg["hex_dump"] is False


def test_set_config(core):
    cfg = core.set_config(device="STM32H743", echo_send=True, hex_dump=True)
    assert cfg["device"] == "STM32H743"
    assert cfg["echo_send"] is True
    assert cfg["hex_dump"] is True


def test_gui_config_persist(tmp_config_dir, core):
    core.save_gui_config(device="STM32F407VE", devices=["STM32F407VE", "STM32H743"])
    # 重新加载
    data = core.load_gui_config()
    assert data["last_device"] == "STM32F407VE"
    assert "STM32H743" in data["devices"]
    assert os.path.exists(tmp_config_dir / "rtt_history.json")


def test_agent_config_separate_file(tmp_config_dir, core):
    """agent 配置应存到独立文件 agent_config.json，与 GUI 配置分离。"""
    core.save_agent_config({"port": 7000, "token": "abc"})
    assert os.path.exists(tmp_config_dir / "agent_config.json")
    data = core.load_agent_config()
    assert data["port"] == 7000
    # GUI 配置文件不应包含 agent 字段
    gui = core.load_gui_config()
    assert "port" not in gui


def test_agent_history_separate_file(tmp_config_dir, core):
    """agent 命令历史应存到独立文件 agent_history.json。"""
    core.append_agent_history("connect: STM32F407VE/SWD")
    core.append_agent_history("send: led on")
    core.save_agent_history()
    assert os.path.exists(tmp_config_dir / "agent_history.json")

    # 重新加载
    core2 = RTTCore()
    hist = core2.load_agent_history()
    assert "connect: STM32F407VE/SWD" in hist
    assert "send: led on" in hist


def test_agent_history_dedup_and_limit(tmp_config_dir, core):
    # 相邻重复应去重
    for _ in range(3):
        core.append_agent_history("send: ping")
    assert core.get_agent_history().count("send: ping") == 1

    # 限长
    core2 = RTTCore()
    for i in range(2000):
        core2.append_agent_history(f"cmd_{i}")
    assert len(core2.get_agent_history(limit=10000)) <= 1000


# ── 事件总线 ────────────────────────────────────────────────
def test_subscribe_and_unsubscribe(core):
    received = []
    sid = core.subscribe(lambda t, p: received.append((t, p)))
    core._emit(EVENT_OUTPUT, {"text": "hello"})
    assert received == [("output", {"text": "hello"})]
    core.unsubscribe(sid)
    received.clear()
    core._emit(EVENT_OUTPUT, {"text": "world"})
    assert received == []


def test_subscriber_exception_does_not_block_others(core):
    """一个订阅者抛异常不应影响其他订阅者。"""
    good = []
    def bad(t, p):
        raise RuntimeError("boom")
    core.subscribe(bad)
    core.subscribe(lambda t, p: good.append(p))
    core._emit(EVENT_OUTPUT, {"text": "x"})
    assert good == [{"text": "x"}]


# ── 输出缓冲 ────────────────────────────────────────────────
def test_get_output_incremental(core):
    core._emit_output("a")
    core._emit_output("b")
    core._emit_output("c")
    lines, cursor = core.get_output()
    assert "".join(lines) == "abc"
    # 用 cursor 增量取
    lines2, cursor2 = core.get_output(since=cursor)
    assert lines2 == []
    core._emit_output("d")
    lines3, cursor3 = core.get_output(since=cursor)
    assert lines3 == ["d"]


def test_get_output_clear(core):
    core._emit_output("a")
    core._emit_output("b")
    lines, cursor = core.get_output(clear=True)
    assert "".join(lines) == "ab"
    # 清除后新数据从头计
    core._emit_output("c")
    lines2, _ = core.get_output(since=cursor)
    assert lines2 == ["c"]


def test_clear_output(core):
    core._emit_output("data")
    core.clear_output()
    lines, _ = core.get_output()
    # clear_output 会发一条 [系统] 提示
    assert all("data" not in l for l in lines)


# ── 连接/发送（mock pylink） ────────────────────────────────
def _patched_connect(core, fake_jlink):
    """在 patched pylink 下执行 connect。"""
    with mock.patch.object(core_mod, "pylink") as pl:
        pl.JLink.return_value = fake_jlink
        pl.enums.JLinkInterfaces.SWD = "SWD"
        pl.enums.JLinkInterfaces.JTAG = "JTAG"
        core.connect(device="STM32F407VE")


def test_connect_emits_status_and_output(core, fake_jlink):
    events = []
    core.subscribe(lambda t, p: events.append((t, p)))
    _patched_connect(core, fake_jlink)
    types = [e[0] for e in events]
    assert EVENT_OUTPUT in types   # "连接成功" 提示
    assert EVENT_STATUS in types
    assert ("status", {"connected": True}) in events
    assert core.is_connected is True


def test_send_requires_connection(core):
    with pytest.raises(RTTError):
        core.send("hello")


def test_send_writes_bytes(core, fake_jlink):
    _patched_connect(core, fake_jlink)
    n = core.send("led on")
    # "led on\n" = 7 字节
    assert n == 7
    fake_jlink.rtt_write.assert_called()
    args = fake_jlink.rtt_write.call_args
    assert args[0][0] == 0  # channel 0


def test_send_echo(core, fake_jlink):
    core.set_config(echo_send=True)
    received = []
    core.subscribe(lambda t, p: received.append((t, p)))
    _patched_connect(core, fake_jlink)
    received.clear()
    core.send("ping")
    outs = [p["text"] for t, p in received if t == EVENT_OUTPUT]
    assert any("发送" in o for o in outs)


def test_disconnect(core, fake_jlink):
    _patched_connect(core, fake_jlink)
    assert core.is_connected
    core.disconnect()
    assert core.is_connected is False
    fake_jlink.rtt_stop.assert_called()
    fake_jlink.close.assert_called()


def test_read_loop_broadcasts_output(core, fake_jlink):
    """读取线程应把 rtt_read 的数据解码后广播。"""
    fake_jlink.rtt_read.return_value = list(b"hello\xff")
    # \xff 在 UTF-8 下 decode(errors='replace') -> 替换字符
    received = []
    core.subscribe(lambda t, p: received.append(p.get("text", "")))
    _patched_connect(core, fake_jlink)
    # 等读取线程跑几轮
    time.sleep(0.1)
    core.disconnect()
    joined = "".join(received)
    assert "hello" in joined


def test_connect_failure_raises_rtt_error(core):
    """JLink.open 抛异常时应抛 RTTError 而非原始异常。"""
    bad = mock.MagicMock()
    bad.open.side_effect = RuntimeError("no jlink found")
    with mock.patch.object(core_mod, "pylink") as pl:
        pl.JLink.return_value = bad
        pl.enums.JLinkInterfaces.SWD = "SWD"
        with pytest.raises(RTTError):
            core.connect(device="STM32F407VE")
    assert core.is_connected is False
