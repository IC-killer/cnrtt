#!/usr/bin/env python3
"""cnrtt Helper Script for CodeBuddy Skill.

Provides command-line interface for common cnrtt operations.
Usage:
    python cnrtt_helper.py <command> [options]

Commands:
    status          - Check cnrtt agent server status
    connect         - Connect to target device
    disconnect      - Disconnect from target
    send            - Send text to RTT channel
    target_help     - Send default target help command (k:help) and read response
    get_output      - Get RTT output
    watch           - Watch RTT output continuously
    config          - Get/set configuration
"""

import argparse
import json
import sys
import threading
import time

try:
    from cnrtt.agent_client import AgentClient, AgentError
except ImportError:
    print("Error: cnrtt not installed. Install with: pip install cnrtt", file=sys.stderr)
    sys.exit(1)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7000
DEFAULT_TARGET_HELP_COMMAND = "k:help"


def cmd_status(args):
    """Check cnrtt status."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        result = client.call("status")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_connect(args):
    """Connect to target device."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        params = {}
        if args.device:
            params["device"] = args.device
        if args.iface:
            params["iface"] = args.iface
        if args.charset:
            params["charset"] = args.charset
        
        result = client.call("connect", params)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("Connected successfully!")
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_disconnect(args):
    """Disconnect from target."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        result = client.call("disconnect")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_send(args):
    """Send text to RTT channel."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        params = {"text": args.text}
        if args.no_newline:
            params["append_newline"] = False
        
        result = client.call("send", params)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_target_help(args):
    """Send the default target help command and print the response."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        send_result = client.call(
            "send",
            {"text": args.text, "append_newline": not args.no_newline},
        )
        if args.delay > 0:
            time.sleep(args.delay)
        output = client.call("get_output", {"limit": args.limit})
        result = {"send": send_result, "output": output}
        print(json.dumps(result, indent=2, ensure_ascii=False))

        if output.get("lines"):
            print("\n--- Target Help ---")
            for line in output["lines"]:
                print(line, end="")
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_get_output(args):
    """Get RTT output."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        params = {}
        if args.since:
            params["since"] = args.since
        if args.limit:
            params["limit"] = args.limit
        if args.clear:
            params["clear"] = True
        
        result = client.call("get_output", params)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Also print the lines for easy reading
        if result.get("lines"):
            print("\n--- Output Lines ---")
            for line in result["lines"]:
                print(line, end="")
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_watch(args):
    """Watch RTT output continuously."""
    client = AgentClient(args.host, args.port, token=args.token)
    
    stop = threading.Event()
    
    def on_output(text):
        if args.raw:
            print(json.dumps({"output": text}, ensure_ascii=False), flush=True)
        else:
            print(text, end="", flush=True)
    
    def on_status(connected):
        status = "connected" if connected else "disconnected"
        print(f"\n[Status: {status}]", flush=True)
    
    def on_error(message):
        print(f"\n[Error: {message}]", flush=True)
    
    try:
        print(f"Watching RTT output... (Press Ctrl+C to stop)")
        client.watch(
            on_output=on_output,
            on_status=on_status,
            on_error=on_error,
            stop_event=stop
        )
    except KeyboardInterrupt:
        print("\nStopping...")
        stop.set()
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cmd_config(args):
    """Get/set configuration."""
    client = AgentClient(args.host, args.port, token=args.token)
    try:
        if args.set:
            # Parse key=value pairs
            params = {}
            for kv in args.set:
                if "=" not in kv:
                    print(f"Error: Invalid format '{kv}'. Use key=value", file=sys.stderr)
                    return 1
                k, v = kv.split("=", 1)
                # Convert string values
                if v.lower() in ("true", "false"):
                    params[k] = v.lower() == "true"
                else:
                    params[k] = v
            
            result = client.call("set_config", params)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            result = client.call("get_config")
            print(json.dumps(result, indent=2, ensure_ascii=False))
        
        if args.save:
            client.call("save_config")
            print("Config saved.")
    except AgentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="cnrtt_helper",
        description="cnrtt Helper Script for CodeBuddy"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="cnrtt server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="cnrtt server port")
    parser.add_argument("--token", default=None, help="Authentication token")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # status
    subparsers.add_parser("status", help="Check cnrtt status")
    
    # connect
    p_connect = subparsers.add_parser("connect", help="Connect to target device")
    p_connect.add_argument("--device", help="Target device name (e.g., STM32F407VE)")
    p_connect.add_argument("--iface", choices=["SWD", "JTAG"], help="Debug interface")
    p_connect.add_argument("--charset", choices=["UTF-8", "GB2312"], help="Character set")
    
    # disconnect
    subparsers.add_parser("disconnect", help="Disconnect from target")
    
    # send
    p_send = subparsers.add_parser("send", help="Send text to RTT channel")
    p_send.add_argument("--text", required=True, help="Text to send")
    p_send.add_argument("--no-newline", action="store_true", help="Don't append newline")
    
    # target_help
    p_target_help = subparsers.add_parser(
        "target_help",
        help="Send the default target help command (k:help) and read response",
    )
    p_target_help.add_argument(
        "--text",
        default=DEFAULT_TARGET_HELP_COMMAND,
        help="Target help command to send",
    )
    p_target_help.add_argument("--no-newline", action="store_true", help="Don't append newline")
    p_target_help.add_argument("--delay", type=float, default=0.2, help="Seconds to wait before reading output")
    p_target_help.add_argument("--limit", type=int, default=200, help="Max output entries to read")

    # get_output
    p_get = subparsers.add_parser("get_output", help="Get RTT output")
    p_get.add_argument("--since", type=int, help="Cursor to start from")
    p_get.add_argument("--limit", type=int, default=100, help="Max number of lines")
    p_get.add_argument("--clear", action="store_true", help="Clear output buffer after reading")
    
    # watch
    p_watch = subparsers.add_parser("watch", help="Watch RTT output continuously")
    p_watch.add_argument("--raw", action="store_true", help="Output raw JSON")

    # config
    p_config = subparsers.add_parser("config", help="Get/set configuration")
    p_config.add_argument("--set", nargs="*", help="Set config: key=value key2=value2")
    p_config.add_argument("--save", action="store_true", help="Save config to file")
    
    args = parser.parse_args()
    
    # Dispatch to command handler
    handlers = {
        "status": cmd_status,
        "connect": cmd_connect,
        "disconnect": cmd_disconnect,
        "send": cmd_send,
        "target_help": cmd_target_help,
        "get_output": cmd_get_output,
        "watch": cmd_watch,
        "config": cmd_config,
    }
    
    handler = handlers.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
