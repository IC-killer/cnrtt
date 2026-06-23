"""cnrtt 包的冒烟测试：仅验证可导入、入口函数存在。"""

import tkinter as tk
from unittest import mock

import cnrtt
from cnrtt.app import RTTViewerApp


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

        # 清屏方法
        assert hasattr(app, "clear_output")
        assert callable(app.clear_output)
    finally:
        root.destroy()
