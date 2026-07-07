# cnrtt

> 一个支持中文及色彩的第三方 RTT (Real-Time Transfer) 客户端，基于 SEGGER J-Link，使用 UTF-8 编码。

`cnrtt` 是一个基于 Python + Tkinter 的桌面 GUI 工具，用于通过 SEGGER J-Link 调试器与目标 MCU 上的 RTT 通道进行双向通信。相比官方 RTT Viewer，它解决了**中文显示乱码**的问题，并支持完整的 **ANSI 颜色转义序列**渲染，让嵌入式日志输出在桌面端也能拥有彩色高亮。

## 特性

- **UTF-8 编码**：正确显示中文及多字节字符，告别乱码
- **ANSI 颜色支持**：解析 `\x1b[1;36m` 等转义序列，16 色前景 + 粗体样式
- **SWD / JTAG 双接口**：通过下拉框切换
- **设备型号记忆**：自动保存最近使用过的设备型号到 `~/.cnrtt/rtt_history.json`
- **双向通信**：可读取 MCU 上行日志，也可向 MCU 下行发送字符串
- **AI 协同调试**：主窗口内置 AI Agent server 开关、监听状态和端口配置，人工与 AI 共享同一个 RTT core
- **Agent 协议**：提供 JSON-RPC 2.0 over TCP 控制接口，便于 AI agent 查询状态、连接目标板、发送命令和读取日志
- **目标板命令发现**：默认目标板支持 `k:help` 指令，连接后可先发送该命令获取固件侧命令列表
- **只读保护**：输出框拦截键盘输入，仅允许复制 / 全选 / 方向键
- **右键复制菜单**：方便复制选中日志
- **跨平台**：Windows / macOS / Linux 均可运行（需有 Tkinter 运行环境）

## 安装

### 从 PyPI 安装（发布后）

```bash
pip install cnrtt
```

### 从本地 wheel 安装

```bash
pip install dist/cnrtt-0.1.0-py3-none-any.whl
```

### 从源码开发安装

```bash
pip install -e .
```

## 使用

安装完成后，在终端直接运行：

```bash
cnrtt
```

或者以模块方式运行：

```bash
python -m cnrtt
```

启动后会弹出 GUI 窗口：

1. 在 **设备型号** 中填入你的 MCU 型号（如 `STM32F407VE`）
2. 选择 **接口**（SWD 或 JTAG）
3. 点击 **连接**，连接成功后即可在中央文本框看到 RTT 输出
4. 在底部输入框输入字符串并回车，可向 MCU 发送数据（自动追加 `\n`）

### AI Agent server

GUI 主窗口中有一行 **AI Agent** 控制区：

- **启用监听**：手动打开或关闭 AI Agent server
- **端口**：配置监听端口，默认 `7000`
- **状态**：显示 `未监听` 或 `监听中 127.0.0.1:<port>`

Agent server 只监听本机地址 `127.0.0.1`，默认端口 `7000`。GUI 和 AI Agent 共用同一个 `RTTCore`，因此人工点击连接、AI 发送命令、AI 读取日志看到的是同一条 RTT 会话。

也可以用命令行启动：

```bash
# GUI 启动，同时自动打开 agent server
python -m cnrtt --with-agent --port 7000

# 无界面模式，仅启动 agent server
python -m cnrtt --headless --port 7000
```

Windows 桌面快捷方式使用 `scripts/launch_cnrtt.vbs` 通过 `pythonw.exe` 无控制台启动，避免弹出黑色控制台窗口。重新生成快捷方式：

```powershell
pwsh -File scripts\create_shortcut.ps1
```

如果任务栏已固定成 Python 默认图标，先在任务栏取消固定旧图标，再运行上面的脚本重新生成快捷方式，然后从新的桌面/开始菜单快捷方式启动或固定。脚本会给快捷方式写入 `cnrtt.rttviewer` AppUserModelID，应用启动时也会给窗口写入同一个任务栏 ID 和重启图标。

#### 手动更换图标

cnrtt 分开使用桌面快捷方式图标和 Tk 窗口/任务栏图标：

- 桌面/开始菜单快捷方式图标：替换 `src/cnrtt/assets/cnrtt.ico`
- 窗口/任务栏图标：替换 `src/cnrtt/assets/cnrtt-32.png` 和 `src/cnrtt/assets/cnrtt-48.png`

推荐准备一个完整的多尺寸 `.ico`（至少包含 16/32/48/256 px），并准备 32x32、48x48 两张 PNG。替换后执行：

```powershell
Copy-Item src\cnrtt\assets\cnrtt.ico build\lib\cnrtt\assets\cnrtt.ico
Copy-Item src\cnrtt\assets\cnrtt-32.png build\lib\cnrtt\assets\cnrtt-32.png
Copy-Item src\cnrtt\assets\cnrtt-48.png build\lib\cnrtt\assets\cnrtt-48.png
pwsh -File scripts\create_shortcut.ps1
```

如果使用 `scripts/make_icon.py` 重新生成内置图标，它会同时输出 `cnrtt.ico`、`cnrtt-32.png` 和 `cnrtt-48.png`。Windows 可能缓存快捷方式图标；替换后如果桌面图标未立即变化，先删除旧快捷方式再运行 `create_shortcut.ps1`，或重新登录 Windows。

### AI 调试默认流程

默认目标板固件支持 `k:help` 指令。连接成功后，AI Agent 应优先发送 `k:help` 获取目标侧命令列表，再根据返回内容继续调试。

```bash
cnrtt-agent-client --port 7000 connect --device STM32F407VE --iface SWD
cnrtt-agent-client --port 7000 send --text "k:help"
cnrtt-agent-client --port 7000 get_output --limit 200
```

项目内置 CodeBuddy skill，路径为 `.codebuddy/skills/cnrtt`。它包含 cnrtt agent 协议速查、常用 helper 脚本和默认 `k:help` 调试流程：

```bash
python .codebuddy/skills/cnrtt/scripts/cnrtt_helper.py target_help
```

## 依赖

- Python ≥ 3.7
- [`pylink-square`](https://pypi.org/project/pylink-square/) — SEGGER J-Link 的 Python 封装
- Tkinter（Python 标准库，部分 Linux 发行版需单独安装 `python3-tk`）

### Linux 安装 Tkinter

```bash
sudo apt-get install python3-tk
```

## 配置文件

GUI 历史设备型号保存在：

- Linux / macOS: `~/.cnrtt/rtt_history.json`
- Windows: `%USERPROFILE%\.cnrtt\rtt_history.json`

Agent server 配置保存在 `~/.cnrtt/agent_config.json`，包括监听端口、host、token 和开关状态。Agent 命令历史保存在 `~/.cnrtt/agent_history.json`。

## ANSI 颜色码对照

| 前景色码 | 颜色   | 前景色码 | 颜色     |
|----------|--------|----------|----------|
| 30       | 黑色   | 90       | 亮黑(灰) |
| 31       | 红色   | 91       | 亮红     |
| 32       | 绿色   | 92       | 亮绿     |
| 33       | 黄色   | 93       | 亮黄     |
| 34       | 蓝色   | 94       | 亮蓝     |
| 35       | 品红   | 95       | 亮品红   |
| 36       | 青色   | 96       | 亮青     |
| 37       | 白色   | 97       | 亮白     |

加粗：在颜色码前加 `1;`，例如 `\x1b[1;32m` 表示亮绿色粗体。

## 开发

### 本地构建

```bash
pip install build
python -m build
```

构建产物位于 `dist/` 目录下：

- `cnrtt-0.1.0-py3-none-any.whl` — wheel 包
- `cnrtt-0.1.0.tar.gz` — 源码包

### 项目结构

```
cnrtt/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/
│   └── cnrtt/
│       ├── __init__.py
│       ├── __main__.py     # 支持 python -m cnrtt
│       ├── app.py          # GUI 主程序 + main() 入口
│       ├── cli.py          # headless / with-agent / GUI 启动入口
│       ├── core.py         # GUI 和 agent server 共用的 RTT core
│       ├── agent_server.py # JSON-RPC agent server
│       └── agent_client.py # agent 参考客户端与 CLI
├── .codebuddy/
│   └── skills/
│       └── cnrtt/          # CodeBuddy skill：RTT 调试工作流与 helper
├── scripts/
│   ├── create_shortcut.ps1 # Windows 无控制台快捷方式生成脚本
│   └── launch_cnrtt.vbs   # Windows pythonw.exe 启动器
└── tests/
```

## License

MIT
