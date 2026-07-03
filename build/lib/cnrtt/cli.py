"""cnrtt 命令行入口：支持 headless / with-agent / 纯 GUI 三种启动模式。

用法：
    cnrtt                                   # 纯 GUI（默认，行为同 cnrtt-gui）
    cnrtt --headless --port 7000            # 纯 headless：core + agent server，无 GUI
    cnrtt --with-agent --port 7000          # GUI + agent server 共享同一 core
    cnrtt --headless --port 7000 --agent-token XXX   # 启用 token 鉴权
"""

from __future__ import annotations

import argparse
import sys
import threading
from typing import Optional

from cnrtt.core import RTTCore


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cnrtt",
        description="cnrtt —— 支持中文及色彩的第三方 RTT 客户端",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--headless",
        action="store_true",
        help="纯无界面模式：仅启动 core + agent server，不创建 Tk 窗口",
    )
    mode.add_argument(
        "--with-agent",
        action="store_true",
        help="GUI + agent server 同时运行，共享同一 core（推荐用于人机协同）",
    )
    p.add_argument("--port", type=int, default=7000, help="agent server 监听端口（默认 7000，可修改）")
    p.add_argument("--host", default="127.0.0.1", help="agent server 监听地址（默认仅本机）")
    p.add_argument("--agent-token", default=None, help="agent 鉴权 token（可选）")
    return p.parse_args(argv)


def _start_agent_server(core: RTTCore, host: str, port: int, token: Optional[str]):
    from cnrtt.agent_server import AgentServer

    server = AgentServer(core, host=host, port=port, token=token)
    server.start()
    print(
        f"[cnrtt] agent server listening on {host}:{port}"
        + (" (with token auth)" if token else ""),
        flush=True,
    )
    return server


def main(argv=None) -> int:
    args = _parse_args(argv)
    core = RTTCore()
    core.load_gui_config()  # 预加载设备历史等

    server = None
    gui_app = None
    gui_root = None

    try:
        if args.headless:
            # 纯 headless：core + agent server，主线程阻塞
            server = _start_agent_server(
                core, args.host, args.port, args.agent_token
            )
            print("[cnrtt] headless mode, Ctrl+C to exit.", flush=True)
            server.serve_forever()
            return 0

        if args.with_agent:
            # GUI + agent server，共享 core
            server = _start_agent_server(
                core, args.host, args.port, args.agent_token
            )
            import tkinter as tk

            from cnrtt.app import RTTViewerApp, _set_window_icon

            gui_root = tk.Tk()
            _set_window_icon(gui_root)
            gui_app = RTTViewerApp(gui_root, core=core)
            gui_root.protocol("WM_DELETE_WINDOW", gui_app.on_closing)
            gui_root.mainloop()
            return 0

        # 默认纯 GUI（不启动 agent server，零端口占用）
        import tkinter as tk

        from cnrtt.app import RTTViewerApp, _set_window_icon

        gui_root = tk.Tk()
        _set_window_icon(gui_root)
        gui_app = RTTViewerApp(gui_root, core=core)
        gui_root.protocol("WM_DELETE_WINDOW", gui_app.on_closing)
        gui_root.mainloop()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.stop()
        if gui_app is not None:
            # on_closing 已由 protocol 触发；兜底清理
            try:
                core.close()
            except Exception:
                pass
        elif core is not None:
            try:
                core.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
