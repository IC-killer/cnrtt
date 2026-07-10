---
name: cnrtt
description: Use this skill when working with cnrtt, SEGGER RTT, J-Link, STM32 or other ARM Cortex-M embedded debugging workflows, especially when an AI agent needs to connect to the cnrtt agent server, read RTT logs, send commands to the target board, or discover target-side commands. The default target firmware supports the `k:help` RTT command for command discovery.
---

# cnrtt - RTT Debugging Tool

## Overview

cnrtt is a Python/Tkinter RTT (Real-Time Transfer) client for SEGGER J-Link debuggers. It provides a GUI for manual debugging and an embedded JSON-RPC agent server so CodeBuddy can share the same RTT connection with the human operator.

Use this skill to:
- Check whether the cnrtt agent server is listening.
- Connect/disconnect a J-Link target.
- Reset, halt, or resume the target through J-Link.
- Read RTT output and watch live logs.
- Send target commands through RTT channel 0.
- Read target memory and manage runtime variable watch items.
- Discover target commands with the default `k:help` command.

## Prerequisites

Before using this skill, ensure:
1. **cnrtt is installed**: `pip install cnrtt`, install from local wheel, or run editable from this repo with `pip install -e .`.
2. **J-Link driver is installed** and accessible.
3. **cnrtt agent server is running**. Prefer the GUI switch for human-AI collaboration:
   - Start cnrtt.
   - In the main window, use the `AI Agent` enable-listening checkbox.
   - Confirm the displayed listening port, normally `127.0.0.1:7000`.

Command-line alternatives:
   ```bash
   # Headless mode
   python -m cnrtt --headless --port 7000
   
   # GUI with agent server enabled at startup
   python -m cnrtt --with-agent --port 7000
   ```

On Windows, the provided `scripts/launch_cnrtt.vbs` and generated shortcuts use `pythonw.exe` through `wscript.exe` to avoid a black console window.

## Default Target Discovery

After connecting to the target, send `k:help` first unless the user gives a more specific command. The default target board firmware supports this command and should return the available command list.

Preferred sequence:

```python
from cnrtt.agent_client import AgentClient

c = AgentClient("127.0.0.1", 7000)
c.call("connect", {"device": "STM32F407VE", "iface": "SWD", "charset": "UTF-8"})
c.call("send", {"text": "k:help", "append_newline": True})
result = c.call("get_output", {"limit": 200})
c.close()
```

Using the bundled helper:

```bash
python .codebuddy/skills/cnrtt/scripts/cnrtt_helper.py target_help
```

## Core Capabilities

### 1. Check cnrtt Status

To check if cnrtt agent server is running and get current state:

```python
from cnrtt.agent_client import AgentClient

c = AgentClient("127.0.0.1", 7000)
result = c.call("status")
c.close()
# Returns: {"device": "...", "iface": "SWD", "charset": "UTF-8", "echo_send": false, "hex_dump": false, "connected": false}
```

### 2. Connect to Target

To connect to an MCU via J-Link:

```python
c.call("connect", {"device": "STM32F407VE", "iface": "SWD", "charset": "UTF-8"})
# Returns: {"connected": true}
```

Common device names: `STM32F103C8`, `STM32F407VE`, `STM32F429ZI`, `NRF52840_XXAA`, etc.

### 3. Send Commands to MCU

To send text to RTT channel 0:

```python
c.call("send", {"text": "k:help", "append_newline": True})
# Returns: {"bytes_sent": 7}
```

Use `k:help` as the default first command to discover target-side commands.

### 4. Control Target State

```python
c.call("reset")  # J-Link reset
c.call("halt")   # J-Link halt / pause
c.call("run")    # J-Link go / resume
```

### 5. Read RTT Output

To read output from the MCU:

```python
# Get recent output
result = c.call("get_output", {"limit": 100})
# Returns: {"lines": ["line1\n", "line2\n"], "next_cursor": 42}

# Get new output since last read
result = c.call("get_output", {"since": last_cursor, "limit": 1000})
```

### 6. Monitor Output in Real-Time

To continuously monitor RTT output (run in separate thread/client):

```python
import threading

def on_output(text):
    print(text, end="", flush=True)

c_watch = AgentClient("127.0.0.1", 7000)
stop = threading.Event()
t = threading.Thread(target=c_watch.watch, kwargs={"on_output": on_output, "stop_event": stop}, daemon=True)
t.start()

# ... do work ...

stop.set()
c_watch.close()
```

**Important**: Use separate `AgentClient` instances for `call()` and `watch()` - they cannot share the same socket.

### 7. Read Memory and Watch Variables

```python
# One-shot online memory read
c.call("read_memory", {"address": "0x20000000", "size": 4})

# Periodic variable watch
c.call("watch_add", {
    "name": "counter",
    "address": "0x20000000",
    "type": "u32",
    "period_ms": 250,
})
c.call("watch_budget_get")
c.call("watch_start")
items = c.call("watch_list")["items"]
c.call("watch_stop")
```

Use `watch_budget_set` to limit sampling pressure:

```python
c.call("watch_budget_set", {
    "max_calls": 32,
    "max_bytes": "0x2000",
    "max_cycle_ms": 10,
    "merge_gap": 16,
})
```

### 8. Disconnect

```python
c.call("disconnect")
# Returns: {"connected": false}
```

## Workflow Examples

### Embedded Debugging Session

```
1. Start cnrtt and enable AI Agent listening in the GUI, or start headless mode
2. Connect to target: connect(device="STM32F407VE")
3. Send default target discovery command: k:help
4. Read the response via get_output() and identify available commands
5. Send test commands and verify responses
6. Use reset/halt/run or memory watch when target state inspection is needed
7. Disconnect when done
```

### Real-Time Log Monitoring

```
1. Connect to target
2. Start watch() in background thread
3. Execute operations on MCU
4. View real-time logs as they arrive
```

## Configuration

To modify cnrtt settings:

```python
# Get current config
config = c.call("get_config")

# Update config
c.call("set_config", {"charset": "GB2312", "echo_send": True})

# Save config permanently
c.call("save_config")
```

## Error Handling

Common error codes:
- `-32001`: Not connected (call `connect` first)
- `-32603`: Internal error (check J-Link connection)
- `-32002`: Authentication failed (provide token if server requires it)

## Using the Helper Script

For common operations, use the bundled helper script:

```bash
# Check status
python scripts/cnrtt_helper.py status

# Connect to device
python scripts/cnrtt_helper.py connect --device STM32F407VE

# Send command
python scripts/cnrtt_helper.py send --text "led on"

# Discover default target commands
python scripts/cnrtt_helper.py target_help

# Get output
python scripts/cnrtt_helper.py get_output --limit 100

# Watch output continuously
python scripts/cnrtt_helper.py watch
```

## Resources

- **scripts/cnrtt_helper.py**: Helper script for common cnrtt operations
- **references/agent_protocol.md**: Full JSON-RPC protocol specification
- **Project's agent_protocol.md**: Complete API documentation at project root
