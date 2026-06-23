"""cnrtt 的主程序模块，包含 RTTViewerApp 及其入口函数。"""

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk

import pylink

CONFIG_FILE = os.path.join(
    os.path.expanduser("~"), ".cnrtt", "rtt_history.json"
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


class RTTViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CN RTT Viewer (支持中文及色彩)")
        self.root.geometry("700x500")

        self.jlink = None
        self.rtt_thread = None
        self.is_connected = False
        self.msg_queue = queue.Queue()

        # 加载历史记录
        self.history = self.load_history()

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

        # 中部日志输出区
        mid_frame = tk.Frame(root, padx=10)
        mid_frame.pack(fill=tk.BOTH, expand=True)

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

        self.send_btn = tk.Button(bottom_frame, text="发送", command=self.send_input)
        self.send_btn.pack(side=tk.LEFT, padx=10)

        # 用于保存不完整的 ANSI 序列
        self.ansi_buffer = ""
        # 当前激活的颜色样式
        self.current_style = None

        # 启动UI更新轮询
        self.process_queue()

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
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"last_device": "STM32F407VE", "devices": ["STM32F407VE"]}

    def save_history(self):
        current_device = self.device_var.get().strip()
        if not current_device:
            return

        devices = self.history.get("devices", [])
        if current_device not in devices:
            devices.append(current_device)

        self.history["devices"] = devices
        self.history["last_device"] = current_device
        self.history["last_charset"] = self.charset_var.get()

        self.device_combo['values'] = devices

        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def toggle_connection(self):
        if not self.is_connected:
            self.connect()
        else:
            self.disconnect()

    def connect(self):
        try:
            self.jlink = pylink.JLink()
            self.jlink.open()

            iface = pylink.enums.JLinkInterfaces.SWD if self.iface_var.get() == "SWD" else pylink.enums.JLinkInterfaces.JTAG

            try:
                self.jlink.set_tif(iface)
            except AttributeError:
                pass

            self.jlink.connect(self.device_var.get())
            self.jlink.rtt_start()
            time.sleep(0.2)

            self.is_connected = True
            self.connect_btn.config(text="断开")
            self.device_combo.config(state=tk.DISABLED)
            self.iface_combo.config(state=tk.DISABLED)
            self.charset_combo.config(state=tk.DISABLED)
            self.save_history()

            self.rtt_thread = threading.Thread(target=self.read_rtt_loop, daemon=True)
            self.rtt_thread.start()

            self.append_output("[系统] 连接成功，RTT 已启动。\n")

        except Exception as e:
            self.append_output(f"[系统错误] {str(e)}\n")
            if self.jlink:
                try:
                    self.jlink.close()
                except Exception:
                    pass

    def disconnect(self):
        self.is_connected = False
        if self.rtt_thread:
            self.rtt_thread.join(timeout=1.0)

        if self.jlink:
            try:
                if self.jlink.connected():
                    self.jlink.rtt_stop()
                self.jlink.close()
            except Exception:
                pass
            self.jlink = None

        self.connect_btn.config(text="连接")
        self.device_combo.config(state="normal")
        self.iface_combo.config(state="readonly")
        self.charset_combo.config(state="readonly")
        self.append_output("[系统] 已断开连接。\n")

    def read_rtt_loop(self):
        charset = self.charset_var.get()
        while self.is_connected and self.jlink:
            try:
                data = self.jlink.rtt_read(0, 1024)
                if data:
                    byte_data = bytes(data)
                    text = byte_data.decode(charset, errors='replace')
                    self.msg_queue.put(text)
            except Exception as e:
                self.msg_queue.put(f"[读取错误] {str(e)}\n")
                self.is_connected = False
                break
            threading.Event().wait(0.01)

    def send_input(self, event=None):
        if not self.is_connected or not self.jlink:
            return

        text = self.input_entry.get()
        if not text:
            return

        charset = self.charset_var.get()
        send_data = (text + '\n').encode(charset, errors='replace')

        try:
            self.jlink.rtt_write(0, list(send_data))
            self.input_entry.delete(0, tk.END)
        except Exception as e:
            self.append_output(f"[发送错误] {str(e)}\n")

    def process_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self.append_output(msg)
        except queue.Empty:
            pass
        self.root.after(50, self.process_queue)

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
        self.disconnect()
        self.root.destroy()


def main():
    """cnrtt 命令行入口：启动 RTT Viewer GUI。"""
    root = tk.Tk()
    app = RTTViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
