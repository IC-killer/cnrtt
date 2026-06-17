# cnrtt

> 一个支持中文及色彩的第三方 RTT (Real-Time Transfer) 客户端，基于 SEGGER J-Link，使用 UTF-8 编码。

`cnrtt` 是一个基于 Python + Tkinter 的桌面 GUI 工具，用于通过 SEGGER J-Link 调试器与目标 MCU 上的 RTT 通道进行双向通信。相比官方 RTT Viewer，它解决了**中文显示乱码**的问题，并支持完整的 **ANSI 颜色转义序列**渲染，让嵌入式日志输出在桌面端也能拥有彩色高亮。

## 特性

- **UTF-8 编码**：正确显示中文及多字节字符，告别乱码
- **ANSI 颜色支持**：解析 `\x1b[1;36m` 等转义序列，16 色前景 + 粗体样式
- **SWD / JTAG 双接口**：通过下拉框切换
- **设备型号记忆**：自动保存最近使用过的设备型号到 `~/.cnrtt/rtt_history.json`
- **双向通信**：可读取 MCU 上行日志，也可向 MCU 下行发送字符串
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

## 依赖

- Python ≥ 3.7
- [`pylink-square`](https://pypi.org/project/pylink-square/) — SEGGER J-Link 的 Python 封装
- Tkinter（Python 标准库，部分 Linux 发行版需单独安装 `python3-tk`）

### Linux 安装 Tkinter

```bash
sudo apt-get install python3-tk
```

## 配置文件

历史设备型号保存在：

- Linux / macOS: `~/.cnrtt/rtt_history.json`
- Windows: `%USERPROFILE%\.cnrtt\rtt_history.json`

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
│       └── app.py          # 主程序 + main() 入口
└── tests/
```

## License

MIT
