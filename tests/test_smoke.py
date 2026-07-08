"""cnrtt 包的冒烟测试：仅验证可导入、入口函数存在。"""

import tkinter as tk
from unittest import mock

import cnrtt
from cnrtt.app import RTTViewerApp
from cnrtt.core import RTTCore


def test_version():
    assert isinstance(cnrtt.__version__, str)
    assert cnrtt.__version__ == "0.1.0"


def test_main_callable():
    assert callable(cnrtt.main)


def test_app_class_importable():
    assert hasattr(cnrtt, "RTTViewerApp")


def test_gui_components():
    """验证 GUI 组件：字符集下拉框、输入历史、显示发送选项"""
    root = tk.Tk()
    try:
        with mock.patch.object(RTTViewerApp, 'load_history', return_value={"last_device": "STM32F407VE", "devices": ["STM32F407VE"]}):
            with mock.patch.object(RTTCore, "load_agent_config", return_value={}):
                app = RTTViewerApp(root)

        # 字符集下拉框
        assert hasattr(app, "charset_var")
        assert hasattr(app, "charset_combo")
        assert app.charset_var.get() == "UTF-8"
        values = app.charset_combo.cget("values")
        assert "UTF-8" in values
        assert "GB2312" in values

        # 输入历史
        assert hasattr(app, "input_history")
        assert isinstance(app.input_history, list)
        assert app.input_history_idx == -1

        # 显示发送选项
        assert hasattr(app, "echo_send_var")
        assert app.echo_send_var.get() is False

        # Hex Dump 选项
        assert hasattr(app, "hex_dump_var")
        assert app.hex_dump_var.get() is False

        # J-Link 状态标签
        assert hasattr(app, "jlink_status_var")
        assert "J-Link" in app.jlink_status_var.get()
        app._refresh_jlink_status(
            {
                "connected": False,
                "jlink_status": "重连中",
                "jlink_status_detail": "自动重连 1/3",
            }
        )
        assert "重连中" in app.jlink_status_var.get()
        assert "自动重连 1/3" in app.jlink_status_var.get()

        # 变量监控控件
        assert hasattr(app, "watch_toggle_btn")
        assert hasattr(app, "load_axf_btn")
        assert hasattr(app, "watch_run_btn")
        assert hasattr(app, "watch_tree")
        assert app.watch_run_btn.cget("text") == "开始采样"

        app._refresh_watch_table(
            [
                {
                    "id": "w1",
                    "enabled": True,
                    "name": "counter",
                    "address_hex": "0x20000000",
                    "type": "u32",
                    "period_ms": 250,
                    "value": "1 (0x00000001)",
                    "error": "",
                }
            ]
        )
        app.watch_tree.selection_set("w1")
        copied = app.copy_watch_selection()
        assert "名称" in copied
        assert "counter" in copied
        assert "1 (0x00000001)" in copied

        # 清屏方法
        assert hasattr(app, "clear_output")
        assert callable(app.clear_output)
        assert hasattr(app, "clear_output_btn")
        assert hasattr(app, "save_output_btn")
        assert hasattr(app, "pause_scroll_btn")
        assert app.pause_scroll_btn.cget("text") == "暂停滚动"
        assert app.output_scroll_paused is False
        assert hasattr(app, "bottom_frame")
        assert app.bottom_frame.pack_info()["side"] == "bottom"
        assert int(app.output_text.cget("height")) == 8

        app.toggle_output_scroll()
        assert app.output_scroll_paused is True
        assert app.pause_scroll_btn.cget("text") == "恢复滚动"
        app.toggle_output_scroll()
        assert app.output_scroll_paused is False
        assert app.pause_scroll_btn.cget("text") == "暂停滚动"

        app.output_text.insert(tk.END, "old output")
        with mock.patch.object(app.core, "clear_output") as clear_output:
            app.clear_output()
            clear_output.assert_called_once_with(announce=False)
        assert app.output_text.get("1.0", "end-1c") == ""

        # AI agent server 控件
        assert hasattr(app, "agent_status_var")
        assert hasattr(app, "agent_port_var")
        assert hasattr(app, "agent_toggle")
        assert app.agent_port_var.get() == "7000"
        assert "未监听" in app.agent_status_var.get()
    finally:
        root.destroy()


def test_manual_watch_item_adds_via_core():
    """GUI 手动添加变量应调用 core 的 watch API。"""
    root = tk.Tk()
    try:
        with mock.patch.object(RTTViewerApp, 'load_history', return_value={"last_device": "STM32F407VE", "devices": ["STM32F407VE"]}):
            with mock.patch.object(RTTCore, "load_agent_config", return_value={}):
                app = RTTViewerApp(root)

        with mock.patch.object(app, "save_history"):
            with mock.patch.object(
                app.core,
                "add_watch_item",
                return_value={"id": "w1", "name": "counter"},
            ) as add_watch_item:
                app.watch_name_var.set("counter")
                app.watch_addr_var.set("0x20000000")
                app.watch_type_var.set("u32")
                app.watch_period_var.set("250")

                assert app.add_watch_item()["name"] == "counter"

        add_watch_item.assert_called_once_with(
            name="counter",
            address=0x20000000,
            value_type="u32",
            period_ms=250,
            enabled=True,
            source="manual",
        )
    finally:
        root.destroy()


def test_axf_load_syncs_existing_axf_watch_items():
    """重新加载 AXF 后，应刷新已有 AXF 来源采样项的地址。"""
    root = tk.Tk()
    try:
        with mock.patch.object(RTTViewerApp, 'load_history', return_value={"last_device": "STM32F407VE", "devices": ["STM32F407VE"]}):
            with mock.patch.object(RTTCore, "load_agent_config", return_value={}):
                app = RTTViewerApp(root)

        app.core.replace_watch_items(
            [
                {
                    "id": "w1",
                    "name": "uwTick",
                    "address": 4,
                    "type": "u32",
                    "period_ms": 500,
                    "enabled": True,
                    "source": "axf",
                }
            ]
        )
        app.watch_symbol_by_name = {
            "uwTick": {
                "name": "uwTick",
                "address": 0x20000000,
                "address_hex": "0x20000000",
                "type": "u32",
            }
        }

        assert app._sync_axf_watch_items() == 1
        items = app.core.list_watch_items(include_runtime=False)
        assert items[0]["address"] == 0x20000000
        assert items[0]["address_hex"] == "0x20000000"
    finally:
        root.destroy()


def test_save_output_writes_text_file(tmp_path):
    """保存输出按钮应把当前输出框内容写入文本文件。"""
    root = tk.Tk()
    try:
        output_path = tmp_path / "cnrtt-output.txt"
        with mock.patch.object(RTTViewerApp, 'load_history', return_value={"last_device": "STM32F407VE", "devices": ["STM32F407VE"]}):
            with mock.patch.object(RTTCore, "load_agent_config", return_value={}):
                with mock.patch("cnrtt.app.filedialog.asksaveasfilename", return_value=str(output_path)):
                    app = RTTViewerApp(root)
                    app.output_text.insert(tk.END, "hello\nworld")

                    assert app.save_output() == str(output_path)

        assert output_path.read_text(encoding="utf-8") == "hello\nworld"
    finally:
        root.destroy()


def test_agent_server_controls_start_stop():
    """主窗口应能内嵌启动/停止 agent server。"""
    root = tk.Tk()
    try:
        server = mock.MagicMock()
        server.is_running = True
        with mock.patch.object(RTTViewerApp, 'load_history', return_value={"last_device": "STM32F407VE", "devices": ["STM32F407VE"]}):
            with mock.patch.object(RTTCore, "load_agent_config", return_value={}):
                with mock.patch.object(RTTCore, "save_agent_config") as save_agent_config:
                    with mock.patch("cnrtt.agent_server.AgentServer", return_value=server) as server_cls:
                        app = RTTViewerApp(root)
                        app.agent_port_var.set("8123")

                        assert app.start_agent_server() is True
                        server_cls.assert_called_once_with(
                            app.core,
                            host="127.0.0.1",
                            port=8123,
                            token=None,
                        )
                        server.start.assert_called_once()
                        assert app.agent_enabled_var.get() is True
                        assert "127.0.0.1:8123" in app.agent_status_var.get()
                        assert app.agent_port_entry.cget("state") == tk.DISABLED
                        saved_config = save_agent_config.call_args[0][-1]
                        assert saved_config["enabled"] is True
                        assert saved_config["port"] == 8123

                        app.stop_agent_server()
                        server.stop.assert_called_once()
                        assert app.agent_enabled_var.get() is False
                        assert "未监听" in app.agent_status_var.get()
                        assert app.agent_port_entry.cget("state") == tk.NORMAL
                        saved_config = save_agent_config.call_args[0][-1]
                        assert saved_config["enabled"] is False
    finally:
        root.destroy()
