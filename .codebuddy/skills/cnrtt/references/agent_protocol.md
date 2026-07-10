# cnrtt Agent Protocol Quick Reference

## Connection

- **Protocol**: JSON-RPC 2.0 over TCP
- **Default Port**: 7000
- **Host**: 127.0.0.1 (localhost only)

## Starting the Server

```bash
# Headless mode (recommended for AI agent)
python -m cnrtt --headless --port 7000

# GUI + agent server (human-AI collaboration)
python -m cnrtt --with-agent --port 7000

# With authentication
python -m cnrtt --headless --port 7000 --agent-token MY_SECRET
```

## JSON-RPC Methods

### default target command discovery
After `connect`, send `k:help` unless the user gives a more specific target command. The default target board firmware supports `k:help` and should return the available command list.

```json
{"jsonrpc": "2.0", "id": 10, "method": "send", "params": {"text": "k:help", "append_newline": true}}
```

Then fetch recent output:

```json
{"jsonrpc": "2.0", "id": 11, "method": "get_output", "params": {"limit": 200}}
```

### status
Get current state.
```json
{"jsonrpc": "2.0", "id": 1, "method": "status"}
```
Response: `{"device": "...", "iface": "SWD", "charset": "UTF-8", "echo_send": false, "hex_dump": false, "connected": false}`

### connect
Connect to target via J-Link.
```json
{"jsonrpc": "2.0", "id": 2, "method": "connect", "params": {"device": "STM32F407VE", "iface": "SWD"}}
```

### disconnect
Disconnect from target.
```json
{"jsonrpc": "2.0", "id": 3, "method": "disconnect"}
```

### reset / halt / run
Control target state through J-Link.
```json
{"jsonrpc": "2.0", "id": 31, "method": "reset"}
{"jsonrpc": "2.0", "id": 32, "method": "halt"}
{"jsonrpc": "2.0", "id": 33, "method": "run"}
```

### send
Send text to RTT channel 0.
```json
{"jsonrpc": "2.0", "id": 4, "method": "send", "params": {"text": "k:help", "append_newline": true}}
```

### get_output
Retrieve RTT output.
```json
{"jsonrpc": "2.0", "id": 5, "method": "get_output", "params": {"since": 0, "limit": 100, "clear": false}}
```
Response: `{"lines": ["..."], "next_cursor": 42}`

### set_config / get_config
Modify settings.
```json
{"jsonrpc": "2.0", "id": 6, "method": "set_config", "params": {"charset": "UTF-8", "echo_send": true}}
```

### read_memory
Read target memory through the active J-Link session.
```json
{"jsonrpc": "2.0", "id": 7, "method": "read_memory", "params": {"address": "0x20000000", "size": 4}}
```
Response: `{"address": 536870912, "size": 4, "hex": "78 56 34 12", "bytes": [120, 86, 52, 18]}`

### variable watch
Add periodic memory watch items and control sampling.
```json
{"jsonrpc": "2.0", "id": 8, "method": "watch_add", "params": {"name": "counter", "address": "0x20000000", "type": "u32", "period_ms": 250}}
{"jsonrpc": "2.0", "id": 9, "method": "watch_budget_set", "params": {"max_calls": 32, "max_bytes": "0x2000", "max_cycle_ms": 10, "merge_gap": 16}}
{"jsonrpc": "2.0", "id": 10, "method": "watch_start"}
{"jsonrpc": "2.0", "id": 11, "method": "watch_list", "params": {"include_runtime": true}}
{"jsonrpc": "2.0", "id": 12, "method": "watch_stop"}
```

Other watch methods: `watch_remove`, `watch_clear`, `watch_enable`, `watch_stats`, `watch_budget_get`.

## Server Notifications (Push)

These are sent from server to client without request ID:

### output
RTT data received (batched every 50ms).
```json
{"jsonrpc": "2.0", "method": "output", "params": {"text": "..."}}
```

### status
Connection state changed.
```json
{"jsonrpc": "2.0", "method": "status", "params": {"connected": true}}
```

### error
Error occurred.
```json
{"jsonrpc": "2.0", "method": "error", "params": {"message": "..."}}
```

### watch
Variable watch list or sampled values changed.
```json
{"jsonrpc": "2.0", "method": "watch", "params": {"items": [], "running": false, "stats": {}}}
```

## Error Codes

| Code | Meaning |
|------|---------|
| -32700 | JSON parse error |
| -32600 | Invalid request |
| -32601 | Method not found |
| -32602 | Invalid params |
| -32603 | Internal error |
| -32001 | Not connected |
| -32002 | Authentication failed |

## Message Framing

Each message is prefixed with 4-byte big-endian length:
```
+----------------+----------------------------------+
| length (4B BE) |        JSON body (length B)      |
+----------------+----------------------------------+
```

## Important Notes

1. **Separate clients for call() and watch()**: A single `AgentClient` cannot concurrently use `call()` and `watch()` - use two instances.

2. **Output batching**: The `output` notification batches data within 50ms windows to avoid flooding the client.

3. **Watch sampling budget**: Use `watch_budget_set` before `watch_start` when many variables are watched, so J-Link IO does not starve RTT logging.

4. **Authentication**: When server is started with `--agent-token`, all requests must include `"auth": "<token>"` field.

## Full Protocol Documentation

For complete protocol specification, see: `agent_protocol.md` in the project root.
