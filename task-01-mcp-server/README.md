# Task 1: Strict MCP Customer & Refund Server

Official MCP Python SDK 1.x (`mcp>=1.9,<2`) + Pydantic 2. All MCP protocol communication is JSON-RPC over stdio. Logs go to **stderr** only.

## Mac Terminal setup

```bash
cd ~/Downloads/mcp_task1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run the server

```bash
python server.py
```

`INFO Starting MCP server (stdio)` goes to stderr; the server silently waits for requests.
Press Ctrl+C to stop it.

## Run the terminal test suite (no browser required)

```bash
python test_terminal.py
```

The test starts the server automatically, sends real JSON-RPC requests, confirms the two tools, checks invalid argument handling as top-level JSON-RPC `-32602` errors, and parses every stdout line to catch logging contamination.

## Important SDK version behavior

SDK 1.x's `@server.call_tool()` catches `McpError`, returning an `isError: true` tool result instead of a JSON-RPC error. **This project directly registers a low-level `CallToolRequest` handler with the official SDK** to preserve `-32602` for invalid inputs. Tool business failures are separately returned with `isError: true`.

## Demo scope

`get_customer_record` reads in-memory sample records. `trigger_refund` only simulates refunds. Real refunds require authorization, audit logging, a payment provider, idempotency safeguards, and transaction monitoring.
