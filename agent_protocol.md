# cnrtt Agent 协议规范 (v1.0)

cnrtt 提供一个基于 **JSON-RPC 2.0 over TCP** 的本地控制接口，允许 AI agent
（或任何外部程序）完全控制工具的输入、输出与配置。

本协议文档面向 AI agent 集成者。配套的参考客户端见
`src/cnrtt/agent_client.py`（纯 stdlib，可直接 copy 使用）。

---

## 1. 传输层

| 项 | 规范 |
|---|---|
| 协议 | TCP，默认仅监听 `127.0.0.1`（本机） |
| 默认端口 | `7000`，可通过 `--port` 修改 |
| 消息编码 | UTF-8 JSON |
| 分帧 | 每条消息前 **4 字节大端无符号整数**表示 JSON 体长度 |
| 并发 | 支持多连接，每连接独立线程处理 |
| 最大消息 | 8 MiB（超出返回 `-32600`） |

### 分帧示意

```
+----------------+----------------------------------+
| length (4B BE) |        JSON body (length B)      |
+----------------+----------------------------------+
```

### 伪代码（读取一条消息）

```python
import struct, json
def read_msg(sock):
    header = sock.recv_exactly(4)
    (length,) = struct.unpack(">I", header)
    body = sock.recv_exactly(length)
    return json.loads(body.decode("utf-8"))
```

---

## 2. 启动服务端

```bash
# 纯无界面（推荐用于服务器/CI）
python -m cnrtt --headless --port 7000

# GUI + agent server 共享同一 core（人机协同）
python -m cnrtt --with-agent --port 7000

# 启用 token 鉴权
python -m cnrtt --headless --port 7000 --agent-token MY_SECRET
```

启动后输出：

```
[cnrtt] agent server listening on 127.0.0.1:7000
[cnrtt] headless mode, Ctrl+C to exit.
```

---

## 3. JSON-RPC 消息格式

### 请求（client → server）

```jsonc
{
  "jsonrpc": "2.0",
  "id": 1,                 // 整数或字符串，匹配响应用；通知无此字段
  "method": "connect",
  "params": { "device": "STM32F407VE" },
  "auth": "MY_SECRET"      // 仅启用 token 时必需
}
```

### 响应（server → client）

```jsonc
// 成功
{ "jsonrpc": "2.0", "id": 1, "result": { "connected": true } }

// 失败
{ "jsonrpc": "2.0", "id": 1,
  "error": { "code": -32001, "message": "not connected" } }
```

### 通知（server → client，主动推送，**无 id**）

```jsonc
{ "jsonrpc": "2.0", "method": "output", "params": { "text": "..." } }
```

---

## 4. 方法列表

### 4.1 `status` — 查询当前状态

**请求**：无参数

**响应**：
```jsonc
{
  "device": "STM32F407VE",
  "iface": "SWD",
  "charset": "UTF-8",
  "echo_send": false,
  "hex_dump": false,
  "connected": false
}
```

---

### 4.2 `connect` — 连接 J-Link 并启动 RTT

**请求参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `device` | string | 否 | 目标设备型号；省略则用当前配置 |
| `iface` | string | 否 | `"SWD"` 或 `"JTAG"`；省略用当前配置 |
| `charset` | string | 否 | `"UTF-8"` 或 `"GB2312"`；省略用当前配置 |

**响应**：`{ "connected": true }`

**错误**：连接失败返回 `-32603` + 原始 pylink 异常消息。

**示例**：
```jsonc
// →
{"jsonrpc":"2.0","id":1,"method":"connect","params":{"device":"STM32F407VE","iface":"SWD"}}
// ←
{"jsonrpc":"2.0","id":1,"result":{"connected":true}}
// ← 同时推送状态变更
{"jsonrpc":"2.0","method":"status","params":{"connected":true}}
```

---

### 4.3 `disconnect` — 断开

**响应**：`{ "connected": false }`

---

### 4.4 `send` — 发送文本到 RTT 通道 0

**请求参数**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | 是 | 要发送的文本 |
| `append_newline` | bool | 否 | 默认 `true`，自动追加 `\n` |

**响应**：`{ "bytes_sent": 7 }`

**错误**：未连接返回 `-32001`；写入异常返回 `-32603`。

> 发送内容会被记录到 **agent 命令历史**（`~/.cnrtt/agent_history.json`），
> 与 GUI 的人工输入历史分离。

---

### 4.5 `get_output` — 增量拉取输出

**请求参数**：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `since` | int | 0 | 上次返回的 `next_cursor`，仅取其后的新输出 |
| `limit` | int | 10000 | 最多返回多少条 |
| `clear` | bool | false | 取走后是否从服务端缓冲移除 |

**响应**：
```jsonc
{
  "lines": ["line1\n", "line2\n"],
  "next_cursor": 42
}
```

**拉取模式说明**：
- **轮询模式**：定期 `get_output(since=last_cursor)`，无需监听推送。
- **推送模式**：依赖 `output` 通知（见第 5 节），`get_output` 仅作补偿。
- 两种模式可混用：agent 可既监听推送，也周期性 `get_output` 兜底。

---

### 4.6 `clear_output` — 清空输出缓冲

**响应**：`{ "ok": true }`

---

### 4.7 `get_config` / `set_config` — 读写运行期配置

`set_config` 支持的字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `device` | string | 默认设备型号 |
| `iface` | string | `"SWD"` / `"JTAG"` |
| `charset` | string | `"UTF-8"` / `"GB2312"` |
| `echo_send` | bool | 是否回显发送内容 |
| `hex_dump` | bool | 是否显示原始 HEX |

> 已连接时更改 `device/iface/charset` **不会自动重连**，需显式
> `disconnect` + `connect`。

**响应**：返回更新后的完整配置（同 `status` 结构）。

---

### 4.8 `save_config` — 持久化 GUI 配置

把当前配置写入 `~/.cnrtt/rtt_history.json`。

**响应**：`{ "ok": true }`

---

### 4.9 `get_agent_history` / `clear_agent_history` — agent 命令历史

| 方法 | 参数 | 响应 |
|---|---|---|
| `get_agent_history` | `{ "limit": 100 }` | `{ "history": ["connect: ...", "send: ...", ...] }` |
| `clear_agent_history` | 无 | `{ "ok": true }` |

历史存于 `~/.cnrtt/agent_history.json`，与 GUI 输入历史分离。

---

## 5. 服务端推送（通知）

通知无 `id` 字段，agent 收到后无需回响应。

| method | params | 触发时机 |
|---|---|---|
| `output` | `{ "text": "..." }` | RTT 有新数据；**攒批 50ms** 合并发送，避免高频刷死 agent |
| `status` | `{ "connected": bool }` | 连接/断开变化 |
| `error` | `{ "message": "..." }` | 读取/发送等异常 |

### `output` 攒批策略

服务端把 50ms 窗口内的多条输出合并为一条 `output` 通知的 `text` 字段
（字符串拼接）。因此 `text` 可能包含多行，agent 应按行处理而非假定
一条通知对应一行。

---

## 6. 错误码

| code | 含义 |
|---|---|
| `-32700` | JSON 解析错误 |
| `-32600` | 非法请求（非对象、长度非法等） |
| `-32601` | 方法不存在 |
| `-32602` | 参数错误 |
| `-32603` | 内部错误（含 pylink 异常原文） |
| `-32001` | 未连接（`send` 等需连接的方法在断开时返回） |
| `-32002` | 鉴权失败（token 不匹配） |

---

## 7. 鉴权

启动时若指定 `--agent-token <token>`，则每个请求顶层必须带
`"auth": "<token>"`，否则返回 `-32002`。

```jsonc
{"jsonrpc":"2.0","id":1,"method":"status","auth":"MY_SECRET"}
```

通知（推送）不携带 `auth`——它们由服务端主动发出，agent 无需鉴权即可接收。

---

## 8. 配置文件布局

| 文件 | 用途 | 由谁读写 |
|---|---|---|
| `~/.cnrtt/rtt_history.json` | GUI 配置：设备历史、字符集、echo/hex、人工输入历史 | GUI / `save_config` |
| `~/.cnrtt/agent_config.json` | agent 配置（端口、token 等自定义字段） | agent 自行管理 |
| `~/.cnrtt/agent_history.json` | agent 命令历史 | core 自动记录 `connect`/`send` |

> 三份文件**互不干扰**：agent 不会污染 GUI 的人工输入历史，反之亦然。

---

## 9. 最小集成示例（Python）

```python
from cnrtt.agent_client import AgentClient, AgentError

c = AgentClient("127.0.0.1", 7000)
try:
    print(c.call("status"))
    c.call("connect", {"device": "STM32F407VE", "iface": "SWD"})
    c.call("send", {"text": "led on"})

    # 拉取最近输出
    r = c.call("get_output", {"limit": 100})
    for line in r["lines"]:
        print(line, end="")
finally:
    c.close()
```

### 监听推送（用独立 client，避免与 call 冲突）

```python
import threading
c_watch = AgentClient("127.0.0.1", 7000)
stop = threading.Event()

def on_output(text):
    print(text, end="", flush=True)

t = threading.Thread(target=c_watch.watch,
                     kwargs={"on_output": on_output, "stop_event": stop},
                     daemon=True)
t.start()
# ... 主线程用另一个 AgentClient 发送指令 ...
stop.set()
c_watch.close()
```

> **重要约束**：单个 `AgentClient` 实例的 `call()` 与 `watch()` 共用同一
> socket 与接收缓冲，**不可并发调用**。监听推送期间如需发请求，请使用
> 第二个 `AgentClient` 实例。

---

## 10. CLI 调试工具

```bash
# 查询状态
cnrtt-agent-client status

# 连接
cnrtt-agent-client connect --device STM32F407VE --iface SWD

# 发送
cnrtt-agent-client send --text "led on"
cnrtt-agent-client send --text "ping" --no-newline

# 拉取输出
cnrtt-agent-client get_output --limit 100
cnrtt-agent-client get_output --since 42 --clear

# 清空
cnrtt-agent-client clear

# 配置
cnrtt-agent-client config                          # 查询
cnrtt-agent-client config --set echo_send=true hex_dump=false
cnrtt-agent-client config --save

# agent 历史
cnrtt-agent-client history
cnrtt-agent-client clear_history

# 持续监听推送
cnrtt-agent-client watch
cnrtt-agent-client watch --raw    # 原始 JSON 行

# 指定端口 / token
cnrtt-agent-client --port 8000 --token MY_SECRET status
```

---

## 11. 变更日志

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-07-03 | 首版：JSON-RPC 2.0 over TCP + 长度前缀分帧 + token 鉴权 + 攒批推送 |
