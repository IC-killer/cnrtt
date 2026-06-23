"""cnrtt 包的冒烟测试：仅验证可导入、入口函数存在。"""

import tkinter as tk

import cnrtt
from cnrtt.app import RTTViewerApp


def test_version():
    assert isinstance(cnrtt.__version__, str)
    assert cnrtt.__version__ == "0.1.0"


def test_main_callable():
    assert callable(cnrtt.main)


def test_app_class_importable():
    assert hasattr(cnrtt, "RTTViewerApp")


def test_charset_combo_exists():
    """验证字符集下拉框存在且包含 UTF-8 和 GB2312 选项"""
    root = tk.Tk()
    try:
        app = RTTViewerApp(root)
        assert hasattr(app, "charset_var")
        assert hasattr(app, "charset_combo")
        assert app.charset_var.get() == "UTF-8"
        values = app.charset_combo.cget("values")
        assert "UTF-8" in values
        assert "GB2312" in values
    finally:
        root.destroy()


def test_charset_default_utf8():
    """验证默认字符集为 UTF-8"""
    root = tk.Tk()
    try:
        app = RTTViewerApp(root)
        assert app.charset_var.get() == "UTF-8"
    finally:
        root.destroy()
