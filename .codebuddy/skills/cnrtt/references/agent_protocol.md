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

### send
Send text to RTT channel 0.
```json
{"jsonrpc": "2.0", "id": 4, "method": "send", "params": {"text": "help", "append_newline": true}}
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

3. **Authentication**: When server is started with `--agent-token`, all requests must include `"auth": "<token>"` field.

## Full Protocol Documentation

For complete protocol specification, see: `agent_protocol.md` in the project root.
