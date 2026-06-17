"""cnrtt - 一个支持中文及色彩的第三方 RTT Viewer 客户端。

基于 SEGGER J-Link RTT 协议，使用 UTF-8 编码，可在 Tkinter 界面中
正确显示中文以及 ANSI 颜色转义序列。
"""

from cnrtt.app import RTTViewerApp, main

__version__ = "0.1.0"
__all__ = ["RTTViewerApp", "main"]
