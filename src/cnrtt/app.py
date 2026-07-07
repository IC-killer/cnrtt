"""cnrtt 的主程序模块，包含 RTTViewerApp 及其入口函数。

本模块为表现层：仅负责 Tkinter 渲染与用户交互，所有 RTT 业务逻辑
（连接/收发/配置）委托给 cnrtt.core.RTTCore。GUI 通过订阅 core 的事件
总线刷新自身状态，与 AI agent 服务端处于平等地位。
"""

import os
import queue
import re
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, scrolledtext, ttk
from typing import Optional

from cnrtt.core import (
    EVENT_CONFIG,
    EVENT_ERROR,
    EVENT_OUTPUT,
    EVENT_STATUS,
    RTTCore,
    RTTError,
)

# ANSI 颜色码到 RGB 颜色的映射
ANSI_COLORS = {
    '30': '#000000', '31': '#cc0000', '32': '#4e9a06', '33': '#c4a000',
    '34': '#3465a4', '35': '#75507b', '36': '#06989a', '37': '#d3d7cf',
    '90': '#555753', '91': '#ef2929', '92': '#8ae234', '93': '#fce94f',
    '94': '#729fcf', '95': '#ad7fa8', '96': '#34e2e2', '97': '#eeeeec'
}

# 匹配 ANSI 颜色转义序列 (如 \x1b[1;36m)
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

APP_USER_MODEL_ID = "cnrtt.rttviewer"
APP_DISPLAY_NAME = "cnrtt RTT Viewer"


def _hide_console_window():
    """Hide the Windows console when GUI is launched through python.exe."""
    if os.name != "nt":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def _set_windows_app_user_model_id(app_id: str = APP_USER_MODEL_ID):
    """Set the Windows taskbar identity before creating the Tk window."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def _windows_gui_python_executable() -> str:
    """Return pythonw.exe when available so taskbar relaunch stays console-free."""
    import sys

    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "python.exe":
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    return exe


def _windows_relaunch_command(relaunch_args=None) -> str:
    import subprocess
    import sys

    args = list(sys.argv[1:] if relaunch_args is None else relaunch_args)
    return subprocess.list2cmdline([_windows_gui_python_executable(), "-m", "cnrtt", *args])


def _package_icon_path() -> Optional[str]:
    try:
        from importlib.resources import files

        icon_path = str(files("cnrtt").joinpath("assets", "cnrtt.ico"))
        return icon_path if os.path.exists(icon_path) else None
    except Exception:
        return None


def _set_windows_taskbar_relaunch_metadata(
    root,
    app_id: str = APP_USER_MODEL_ID,
    relaunch_args=None,
) -> bool:
    """Tell Windows what command/icon to use when this Tk window is pinned."""
    if os.name != "nt":
        return False

    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        HRESULT = ctypes.c_long

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

        class PROPVARIANT_UNION(ctypes.Union):
            _fields_ = [("pwszVal", wintypes.LPWSTR), ("ptr", ctypes.c_void_p)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("value", PROPVARIANT_UNION),
            ]

        def guid(value: str) -> GUID:
            return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)

        def check_hresult(hr):
            signed = ctypes.c_long(hr).value
            if signed < 0:
                raise OSError(signed)

        root.update_idletasks()
        hwnd = int(root.winfo_id())
        if not hwnd:
            return False

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = HRESULT
        ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
        ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
        ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        ole32.PropVariantClear.restype = HRESULT

        def propvariant_from_string(value: str) -> PROPVARIANT:
            prop = PROPVARIANT()
            text = str(value) + "\0"
            size = len(text) * ctypes.sizeof(ctypes.c_wchar)
            ptr = ole32.CoTaskMemAlloc(size)
            if not ptr:
                raise MemoryError("CoTaskMemAlloc failed")
            ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
            prop.vt = 31  # VT_LPWSTR
            prop.value.ptr = ptr
            return prop

        property_store = ctypes.c_void_p()
        iid_property_store = guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
        check_hresult(
            shell32.SHGetPropertyStoreForWindow(
                wintypes.HWND(hwnd),
                ctypes.byref(iid_property_store),
                ctypes.byref(property_store),
            )
        )
        if not property_store.value:
            return False

        try:
            vtbl = ctypes.cast(
                property_store,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            set_value = ctypes.WINFUNCTYPE(
                HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(PROPERTYKEY),
                ctypes.POINTER(PROPVARIANT),
            )(vtbl[6])
            commit = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)(vtbl[7])
            release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])

            pkey_app_user_model = guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")

            def set_string_property(pid: int, value: Optional[str]):
                if not value:
                    return
                key = PROPERTYKEY(pkey_app_user_model, pid)
                prop = propvariant_from_string(value)
                try:
                    check_hresult(
                        set_value(property_store.value, ctypes.byref(key), ctypes.byref(prop))
                    )
                finally:
                    ole32.PropVariantClear(ctypes.byref(prop))

            icon_path = _package_icon_path()
            set_string_property(5, app_id)
            set_string_property(2, _windows_relaunch_command(relaunch_args))
            set_string_property(3, f"{icon_path},0" if icon_path else None)
            set_string_property(4, APP_DISPLAY_NAME)
            check_hresult(commit(property_store.value))
        finally:
            try:
                release(property_store.value)
            except Exception:
                pass
        return True
    except Exception:
        if os.environ.get("CNRRT_DEBUG_TASKBAR_ICON") == "1":
            import traceback

            traceback.print_exc()
        return False


class RTTViewerApp:
    def __init__(
        self,
        root,
        core: RTTCore = None,
        agent_enabled: Optional[bool] = None,
        agent_host: str = "127.0.0.1",
        agent_port: Optional[int] = None,
        agent_token: Optional[str] = None,
    ):
        self.root = root
        self.root.title("CN RTT Viewer (支持中文及色彩)")
        self.root.geometry("760x540")

        # 业务核心：可由外部注入（GUI+agent 共享同一 core），否则自建
        self.core = core if core is not None else RTTCore()
        self._owns_core = core is None  # 关闭时决定是否销毁

        # 启动期加载 GUI 配置（设备历史等）
        self.history = self.load_history()

        # AI agent server 配置与运行状态。server 由 GUI 自己管理，避免单独
        # 启动监听窗口；--with-agent 仅作为初始打开开关。
        self._agent_server = None
        self._agent_config = self.core.load_agent_config()
        self._agent_host = str(
            agent_host or self._agent_config.get("host", "127.0.0.1")
        )
        self._agent_token = (
            agent_token if agent_token is not None else self._agent_config.get("token")
        )
        try:
            saved_port = self._agent_config.get("port", 7000)
            self._agent_port = self._parse_agent_port(
                agent_port if agent_port is not None else saved_port
            )
        except ValueError:
            self._agent_port = 7000
        self._agent_autostart = bool(
            self._agent_config.get("enabled", False)
            if agent_enabled is None
            else agent_enabled
        )

        # --- UI 布局 ---
        # 顶部连接设置区
        top_frame = tk.Frame(root, padx=10, pady=10)
        top_frame.pack(fill=tk.X)

        tk.Label(top_frame, text="设备型号:").grid(row=0, column=0, padx=5)
        self.device_var = tk.StringVar(value=self.history.get("last_device", "STM32F407VE"))
        self.device_combo = ttk.Combobox(top_frame, textvariable=self.device_var, values=self.history.get("devices", []), width=20)
        self.device_combo.grid(row=0, column=1, padx=5)

        tk.Label(top_frame, text="接口:").grid(row=0, column=2, padx=5)
        self.iface_var = tk.StringVar(value="SWD")
        self.iface_combo = ttk.Combobox(top_frame, textvariable=self.iface_var, values=["SWD", "JTAG"], width=5, state="readonly")
        self.iface_combo.grid(row=0, column=3, padx=5)

        tk.Label(top_frame, text="字符集:").grid(row=0, column=4, padx=5)
        self.charset_var = tk.StringVar(value=self.history.get("last_charset", "UTF-8"))
        self.charset_combo = ttk.Combobox(top_frame, textvariable=self.charset_var, values=["UTF-8", "GB2312"], width=8, state="readonly")
        self.charset_combo.grid(row=0, column=5, padx=5)

        self.connect_btn = tk.Button(top_frame, text="连接", command=self.toggle_connection)
        self.connect_btn.grid(row=0, column=6, padx=10)

        # AI agent server 控制区
        agent_frame = tk.Frame(root, padx=10, pady=4)
        agent_frame.pack(fill=tk.X)

        tk.Label(agent_frame, text="AI Agent:").pack(side=tk.LEFT)
        self.agent_enabled_var = tk.BooleanVar(value=False)
        self.agent_toggle = tk.Checkbutton(
            agent_frame,
            text="启用监听",
            variable=self.agent_enabled_var,
            command=self.toggle_agent_server,
        )
        self.agent_toggle.pack(side=tk.LEFT, padx=(8, 12))

        tk.Label(agent_frame, text="端口:").pack(side=tk.LEFT)
        self.agent_port_var = tk.StringVar(value=str(self._agent_port))
        validate_port = (root.register(self._validate_agent_port_chars), "%P")
        self.agent_port_entry = tk.Entry(
            agent_frame,
            textvariable=self.agent_port_var,
            width=8,
            validate="key",
            validatecommand=validate_port,
        )
        self.agent_port_entry.pack(side=tk.LEFT, padx=(5, 12))
        self.agent_port_entry.bind("<Return>", self.apply_agent_port)
        self.agent_port_entry.bind("<FocusOut>", self.apply_agent_port)

        self.agent_status_var = tk.StringVar()
        self.agent_status_label = tk.Label(
            agent_frame,
            textvariable=self.agent_status_var,
            anchor="w",
        )
        self.agent_status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 中部日志输出区
        mid_frame = tk.Frame(root, padx=10)
        mid_frame.pack(fill=tk.BOTH, expand=True)

        output_toolbar = tk.Frame(mid_frame)
        output_toolbar.pack(fill=tk.X, pady=(0, 4))

        self.clear_output_btn = tk.Button(
            output_toolbar,
            text="清空",
            command=self.clear_output,
        )
        self.clear_output_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.save_output_btn = tk.Button(
            output_toolbar,
            text="保存",
            command=self.save_output,
        )
        self.save_output_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.pause_scroll_btn = tk.Button(
            output_toolbar,
            text="暂停滚动",
            command=self.toggle_output_scroll,
        )
        self.pause_scroll_btn.pack(side=tk.LEFT)
        self.output_scroll_paused = False

        self.output_text = scrolledtext.ScrolledText(mid_frame, wrap=tk.WORD, font=("Consolas", 10), bg="#1e1e1e", fg="#d4d4d4")
        self.output_text.pack(fill=tk.BOTH, expand=True)

        # 预先配置颜色标签
        self.setup_color_tags()

        # 拦截键盘输入，只允许复制和全选
        self.output_text.bind("<Key>", self.block_editing)

        # 增加右键菜单支持复制
        self.popup_menu = tk.Menu(self.output_text, tearoff=0)
        self.popup_menu.add_command(label="复制", command=self.copy_text)
        self.output_text.bind("<Button-3>", self.show_popup)

        # 底部输入区
        bottom_frame = tk.Frame(root, padx=10, pady=10)
        bottom_frame.pack(fill=tk.X)

        self.input_entry = tk.Entry(bottom_frame, font=("Consolas", 10))
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_entry.bind("<Return>", self.send_input)
        self.input_entry.bind("<Up>", self.input_history_up)
        self.input_entry.bind("<Down>", self.input_history_down)

        self.send_btn = tk.Button(bottom_frame, text="发送", command=self.send_input)
        self.send_btn.pack(side=tk.LEFT, padx=10)

        # 显示发送选项
        self.echo_send_var = tk.BooleanVar(value=self.history.get("echo_send", False))
        self.echo_send_cb = tk.Checkbutton(bottom_frame, text="显示发送", variable=self.echo_send_var, command=self._on_gui_option_changed)
        self.echo_send_cb.pack(side=tk.LEFT, padx=5)

        # 显示原始数据（Hex Dump）选项
        self.hex_dump_var = tk.BooleanVar(value=self.history.get("hex_dump", False))
        self.hex_dump_cb = tk.Checkbutton(bottom_frame, text="Hex", variable=self.hex_dump_var, command=self._on_gui_option_changed)
        self.hex_dump_cb.pack(side=tk.LEFT, padx=5)

        # 输入历史记录（GUI 侧，与 agent 历史分离）
        self.input_history = self.history.get("input_history", [])
        self.input_history_idx = -1  # -1 表示不在历史浏览中

        # 用于保存不完整的 ANSI 序列
        self.ansi_buffer = ""
        # 当前激活的颜色样式
        self.current_style = None

        # 全局快捷键
        root.bind("<F2>", lambda e: self.connect() if not self.core.is_connected else None)
        root.bind("<F3>", lambda e: self.disconnect() if self.core.is_connected else None)
        root.bind("<Alt-r>", lambda e: self.clear_output())
        root.bind("<Alt-R>", lambda e: self.clear_output())
        root.bind("<Activate>", self._on_window_activated)

        # 订阅 core 事件：用线程安全队列把事件 marshal 回 Tk 主线程
        self._event_queue: "queue.Queue" = queue.Queue()
        self._sub_id = self.core.subscribe(self._on_core_event_threadsafe)
        self._process_events()
        self._refresh_agent_ui()
        self.root.after(0, self._focus_input)
        if self._agent_autostart:
            self.root.after(0, self.start_agent_server)

    # ── AI agent server 控制 ─────────────────────────────────
    def _focus_input(self):
        try:
            self.input_entry.focus_set()
            self.input_entry.icursor(tk.END)
        except Exception:
            pass

    def _on_window_activated(self, event=None):
        self.root.after(0, self._focus_input)

    @staticmethod
    def _validate_agent_port_chars(value):
        return value == "" or value.isdigit()

    @staticmethod
    def _parse_agent_port(value) -> int:
        try:
            port = int(str(value).strip())
        except (TypeError, ValueError) as e:
            raise ValueError("端口必须是 1-65535 的整数") from e
        if port < 1 or port > 65535:
            raise ValueError("端口必须是 1-65535 的整数")
        return port

    def _is_agent_server_running(self) -> bool:
        return bool(self._agent_server and self._agent_server.is_running)

    def _set_agent_status(self, text, state="stopped"):
        self.agent_status_var.set(text)
        colors = {
            "running": "#1f7a1f",
            "error": "#b00020",
            "stopped": "#555555",
        }
        self.agent_status_label.config(fg=colors.get(state, "#555555"))

    def _refresh_agent_ui(self):
        running = self._is_agent_server_running()
        self.agent_enabled_var.set(running)
        if running:
            self.agent_port_entry.config(state=tk.DISABLED)
            self._set_agent_status(
                f"监听中 {self._agent_host}:{self._agent_port}",
                state="running",
            )
        else:
            self.agent_port_entry.config(state=tk.NORMAL)
            self._set_agent_status(
                f"未监听，当前端口 {self.agent_port_var.get() or self._agent_port}",
                state="stopped",
            )

    def _save_agent_config(self, enabled: Optional[bool] = None):
        config = dict(self._agent_config)
        config["host"] = self._agent_host
        config["port"] = self._agent_port
        if enabled is not None:
            config["enabled"] = bool(enabled)
        if self._agent_token:
            config["token"] = self._agent_token
        self._agent_config = config
        self.core.save_agent_config(config)

    def apply_agent_port(self, event=None):
        if self._is_agent_server_running():
            return "break"
        try:
            self._agent_port = self._parse_agent_port(self.agent_port_var.get())
        except ValueError as e:
            self._set_agent_status(str(e), state="error")
            return "break"
        self.agent_port_var.set(str(self._agent_port))
        self._save_agent_config()
        self._refresh_agent_ui()
        return "break"

    def toggle_agent_server(self):
        if self.agent_enabled_var.get():
            self.start_agent_server()
        else:
            self.stop_agent_server()

    def start_agent_server(self):
        if self._is_agent_server_running():
            self._refresh_agent_ui()
            return True
        try:
            port = self._parse_agent_port(self.agent_port_var.get())
        except ValueError as e:
            self.agent_enabled_var.set(False)
            self._set_agent_status(str(e), state="error")
            self._save_agent_config(enabled=False)
            return False

        try:
            from cnrtt.agent_server import AgentServer

            server = AgentServer(
                self.core,
                host=self._agent_host,
                port=port,
                token=self._agent_token,
            )
            server.start()
        except Exception as e:
            self._agent_server = None
            self.agent_enabled_var.set(False)
            self._agent_port = port
            self.agent_port_var.set(str(port))
            self._save_agent_config(enabled=False)
            self._set_agent_status(f"启动失败: {e}", state="error")
            self.append_output(f"[Agent] 启动失败: {e}\n")
            return False

        self._agent_server = server
        self._agent_port = port
        self.agent_port_var.set(str(port))
        self._save_agent_config(enabled=True)
        self._refresh_agent_ui()
        self.append_output(
            f"[Agent] server listening on {self._agent_host}:{self._agent_port}\n"
        )
        return True

    def stop_agent_server(self, persist_config: bool = True, announce: bool = True):
        was_running = self._is_agent_server_running()
        if self._agent_server is not None:
            try:
                self._agent_server.stop()
            except Exception:
                pass
        self._agent_server = None
        self.agent_enabled_var.set(False)
        if persist_config:
            try:
                self._agent_port = self._parse_agent_port(self.agent_port_var.get())
            except ValueError:
                pass
            self._save_agent_config(enabled=False)
        self._refresh_agent_ui()
        if announce and was_running:
            self.append_output("[Agent] server stopped.\n")

    # ── 事件回调（先入队列，主线程消费） ──────────────────────
    def _on_core_event_threadsafe(self, event_type, payload):
        """core 回调（可能在读取线程中调用），仅入队，不触碰 Tk。"""
        try:
            self._event_queue.put_nowait((event_type, payload))
        except Exception:
            pass

    def _process_events(self):
        """Tk 主线程轮询事件队列并刷新 GUI。"""
        try:
            while True:
                event_type, payload = self._event_queue.get_nowait()
                self._dispatch_event(event_type, payload)
        except queue.Empty:
            pass
        self.root.after(50, self._process_events)

    def _dispatch_event(self, event_type, payload):
        if event_type == EVENT_OUTPUT:
            self.append_output(payload.get("text", ""))
        elif event_type == EVENT_STATUS:
            self._refresh_connection_ui(payload.get("connected", False))
        elif event_type == EVENT_CONFIG:
            self._sync_config_from_core(payload)
        elif event_type == EVENT_ERROR:
            # 错误已通过 output 事件输出到文本框，此处无需额外处理
            pass

    def _refresh_connection_ui(self, connected):
        if connected:
            self.connect_btn.config(text="断开")
            self.device_combo.config(state=tk.DISABLED)
            self.iface_combo.config(state=tk.DISABLED)
            self.charset_combo.config(state=tk.DISABLED)
        else:
            self.connect_btn.config(text="连接")
            self.device_combo.config(state="normal")
            self.iface_combo.config(state="readonly")
            self.charset_combo.config(state="readonly")

    def _sync_config_from_core(self, cfg):
        """core 配置变更时同步 GUI 控件（避免触发循环，静默设置）。"""
        try:
            if "device" in cfg and cfg["device"] and self.device_var.get() != cfg["device"]:
                self.device_var.set(cfg["device"])
            if "iface" in cfg and cfg["iface"] and self.iface_var.get() != cfg["iface"]:
                self.iface_var.set(cfg["iface"])
            if "charset" in cfg and cfg["charset"] and self.charset_var.get() != cfg["charset"]:
                self.charset_var.set(cfg["charset"])
            if "echo_send" in cfg and self.echo_send_var.get() != cfg["echo_send"]:
                self.echo_send_var.set(bool(cfg["echo_send"]))
            if "hex_dump" in cfg and self.hex_dump_var.get() != cfg["hex_dump"]:
                self.hex_dump_var.set(bool(cfg["hex_dump"]))
        except Exception:
            pass

    def _on_gui_option_changed(self):
        """GUI 复选框变化时同步到 core 并保存配置。"""
        self.core.set_config(
            echo_send=self.echo_send_var.get(),
            hex_dump=self.hex_dump_var.get(),
        )
        self.save_history()

    def setup_color_tags(self):
        """配置 Tkinter 文本框的颜色 Tag"""
        for code, hex_color in ANSI_COLORS.items():
            self.output_text.tag_config(f"col_{code}", foreground=hex_color)
            self.output_text.tag_config(f"col_{code}_bold", foreground=hex_color, font=("Consolas", 10, "bold"))

    def show_popup(self, event):
        """显示右键菜单"""
        try:
            self.popup_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.popup_menu.grab_release()

    def copy_text(self):
        """复制选中的文本"""
        try:
            self.output_text.clipboard_clear()
            selected_text = self.output_text.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.output_text.clipboard_append(selected_text)
        except tk.TclError:
            pass

    def block_editing(self, event):
        """拦截输出框的键盘输入，仅允许复制、全选和方向键"""
        is_ctrl = (event.state & 4) != 0 or (event.state & 8) != 0 or (event.state & 0x14) != 0
        if is_ctrl and event.keysym.lower() in ['c', 'a', 'insert']:
            return None
        if event.keysym in ['Left', 'Right', 'Up', 'Down', 'Home', 'End', 'Prior', 'Next', 'Shift_L', 'Shift_R']:
            return None
        return "break"

    def load_history(self):
        """从 core 加载 GUI 配置（设备历史等）。"""
        return self.core.load_gui_config()

    def save_history(self):
        """保存 GUI 配置到 ~/.cnrtt/rtt_history.json。"""
        self.core.save_gui_config(
            device=self.device_var.get(),
            devices=list(self.history.get("devices", [])),
            input_history=self.input_history,
        )
        # 同步设备下拉列表
        self.device_combo['values'] = self.core.get_gui_devices()

    def toggle_connection(self):
        if not self.core.is_connected:
            self.connect()
        else:
            self.disconnect()

    def connect(self):
        try:
            self.core.connect(
                device=self.device_var.get(),
                iface=self.iface_var.get(),
                charset=self.charset_var.get(),
            )
            self.save_history()
        except RTTError:
            # 错误信息已通过事件输出到文本框
            pass

    def disconnect(self):
        try:
            self.core.disconnect()
        except RTTError:
            pass

    def send_input(self, event=None):
        text = self.input_entry.get()
        if not text:
            return
        try:
            self.core.send(text)
            # 添加到输入历史（去重、限长）
            if not self.input_history or self.input_history[-1] != text:
                self.input_history.append(text)
                if len(self.input_history) > 100:
                    self.input_history.pop(0)
            self.input_history_idx = -1
            self.input_entry.delete(0, tk.END)
            self.save_history()
        except RTTError:
            # 错误已通过事件输出
            pass

    def input_history_up(self, event):
        """上箭头：浏览更早的历史记录"""
        if not self.input_history:
            return "break"

        if self.input_history_idx == -1:
            # 首次进入历史模式，保存当前输入框内容以便恢复
            self._temp_input = self.input_entry.get()
            self.input_history_idx = len(self.input_history) - 1
        elif self.input_history_idx > 0:
            self.input_history_idx -= 1

        self.input_entry.delete(0, tk.END)
        self.input_entry.insert(0, self.input_history[self.input_history_idx])
        return "break"

    def input_history_down(self, event):
        """下箭头：浏览更新的历史记录"""
        if self.input_history_idx == -1:
            return "break"

        if self.input_history_idx < len(self.input_history) - 1:
            self.input_history_idx += 1
            self.input_entry.delete(0, tk.END)
            self.input_entry.insert(0, self.input_history[self.input_history_idx])
        else:
            # 超出最新记录，恢复原始输入内容
            self.input_history_idx = -1
            self.input_entry.delete(0, tk.END)
            self.input_entry.insert(0, getattr(self, '_temp_input', ''))
        return "break"

    def append_output(self, text):
        """向输出框添加文本，支持解析 ANSI 色彩转义码"""
        # 合并上一次未解析完的缓冲
        data = self.ansi_buffer + text

        # 检查末尾是否有不完整的 ANSI 序列，如果有则存入缓冲，等待下次拼接
        last_esc = data.rfind('\x1b')
        if last_esc != -1:
            seq_part = data[last_esc:]
            if 'm' not in seq_part:
                self.ansi_buffer = seq_part
                data = data[:last_esc]
            else:
                self.ansi_buffer = ""
        else:
            self.ansi_buffer = ""

        # 分割文本和 ANSI 控制码
        parts = ANSI_ESCAPE.split(data)
        codes = ANSI_ESCAPE.findall(data)

        for i, part in enumerate(parts):
            if part:
                if self.current_style:
                    self.output_text.insert(tk.END, part, self.current_style)
                else:
                    self.output_text.insert(tk.END, part)

            if i < len(codes):
                self.parse_ansi_code(codes[i])

        if not self.output_scroll_paused:
            self.output_text.see(tk.END)

    def parse_ansi_code(self, code_str):
        """解析 ANSI 颜色控制码并更新当前样式"""
        content = code_str[2:-1]  # 去掉 \x1b[ 和 m
        if not content:
            content = '0'

        codes = content.split(';')

        is_bold = False
        color_code = None

        for c in codes:
            if c == '0':
                is_bold = False
                color_code = None
            elif c == '1':
                is_bold = True
            elif c in ANSI_COLORS:
                color_code = c

        if color_code:
            self.current_style = f"col_{color_code}_bold" if is_bold else f"col_{color_code}"
        else:
            self.current_style = None

    def on_closing(self):
        self.stop_agent_server(persist_config=False, announce=False)
        try:
            self.core.unsubscribe(self._sub_id)
        except Exception:
            pass
        if self._owns_core:
            self.core.close()
        self.root.destroy()

    def clear_output(self):
        """清空输出框"""
        self.output_text.delete("1.0", tk.END)
        self.ansi_buffer = ""
        self.current_style = None
        self.core.clear_output(announce=False)

    def save_output(self):
        """保存输出框内容到文本文件。"""
        default_name = "cnrtt-output-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".txt"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存输出",
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=[
                ("Text files", "*.txt"),
                ("Log files", "*.log"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return None

        text = self.output_text.get("1.0", "end-1c")
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError as e:
            self.append_output(f"[GUI] 保存输出失败: {e}\n")
            return None

        self.append_output(f"[GUI] 输出已保存: {path}\n")
        return path

    def toggle_output_scroll(self):
        """暂停/恢复输出框自动滚动。"""
        self.output_scroll_paused = not self.output_scroll_paused
        if self.output_scroll_paused:
            self.pause_scroll_btn.config(text="恢复滚动")
        else:
            self.pause_scroll_btn.config(text="暂停滚动")
            self.output_text.see(tk.END)
        self._focus_input()
        return "break"


def _set_window_icon(root):
    """为窗口设置任务栏/标题栏图标。

    优先使用包内 32/48px PNG，避免 Tk/Windows 从多尺寸 .ico 中
    选到 256px 图层后在任务栏出现裁剪；.ico 仅作为 fallback。
    找不到时静默跳过，不影响启动。
    """
    try:
        from importlib.resources import files

        assets = files("cnrtt").joinpath("assets")
        png32 = str(assets.joinpath("cnrtt-32.png"))
        png48 = str(assets.joinpath("cnrtt-48.png"))
        icon32 = tk.PhotoImage(file=png32)
        icon48 = tk.PhotoImage(file=png48)
        # Keep references alive; Tk images are garbage-collected otherwise.
        root._cnrtt_icon_images = (icon32, icon48)
        root.iconphoto(True, icon32, icon48)
    except Exception:
        try:
            from importlib.resources import files

            ico_path = str(files("cnrtt").joinpath("assets", "cnrtt.ico"))
            root.iconbitmap(ico_path)
            root.iconbitmap(default=ico_path)
        except Exception:
            # 任何失败（缺文件、Tk 不支持、资源未安装）都不阻断启动
            pass


def main():
    """cnrtt 命令行入口：启动 RTT Viewer GUI。"""
    _hide_console_window()
    _set_windows_app_user_model_id()
    root = tk.Tk()
    _set_window_icon(root)
    _set_windows_taskbar_relaunch_metadata(root)
    app = RTTViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
