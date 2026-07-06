---
name: cnrtt
description: This skill should be used when working with STM32 or other ARM Cortex-M embedded projects that use SEGGER RTT (Real-Time Transfer) for debugging. It enables AI agent to connect to J-Link debuggers, read RTT output from MCU, send commands to the target, and monitor real-time logs. Trigger when the user mentions RTT, J-Link, embedded debugging, STM32 logging, or needs to view/interact with MCU output.
---

# cnrtt - RTT Debugging Tool

## Overview

cnrtt is a Python-based RTT (Real-Time Transfer) client for SEGGER J-Link debuggers. This skill enables CodeBuddy to interact with embedded targets via RTT, allowing reading of MCU logs, sending commands, and real-time monitoring during embedded development.

## Prerequisites

Before using this skill, ensure:
1. **cnrtt is installed**: `pip install cnrtt` or install from local wheel
2. **J-Link driver is installed** and accessible
3. **cnrtt agent server is running** (start with one of these commands):
   ```bash
   # Headless mode (recommended for AI agent)
   python -m cnrtt --headless --port 7000
   
   # GUI + agent server (human-AI collaboration)
   python -m cnrtt --with-agent --port 7000
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
c.call("send", {"text": "help", "append_newline": True})
# Returns: {"bytes_sent": 5}
```

### 4. Read RTT Output

To read output from the MCU:

```python
# Get recent output
result = c.call("get_output", {"limit": 100})
# Returns: {"lines": ["line1\n", "line2\n"], "next_cursor": 42}

# Get new output since last read
result = c.call("get_output", {"since": last_cursor, "limit": 1000})
```

### 5. Monitor Output in Real-Time

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

### 6. Disconnect

```python
c.call("disconnect")
# Returns: {"connected": false}
```

## Workflow Examples

### Embedded Debugging Session

```
1. Start cnrtt agent server (headless mode)
2. Connect to target: connect(device="STM32F407VE")
3. Reset MCU and observe boot logs via get_output()
4. Send test commands and verify responses
5. Disconnect when done
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

# Get output
python scripts/cnrtt_helper.py get_output --limit 100

# Watch output continuously
python scripts/cnrtt_helper.py watch
```

## Resources

- **scripts/cnrtt_helper.py**: Helper script for common cnrtt operations
- **references/agent_protocol.md**: Full JSON-RPC protocol specification
- **Project's agent_protocol.md**: Complete API documentation at project root
