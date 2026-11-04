# Task 2 — MCP Security Gateway Proxy (Tool Filtering & Authentication)

**QuilrAI — AI Field Developer / Solutions Engineer Technical Assessment**

A runnable, minimal **Python 3.11+ HTTP/JSON-RPC reverse proxy** between an AI client and a downstream mock MCP HTTP server. Uses **only the Python standard library**.

## Architecture

```text
AI Agent / curl
   |  POST /mcp + Authorization: Bearer <token> + JSON-RPC 2.0
   v
MCP Security Gateway (127.0.0.1:8000)
   |-- Validate JSON-RPC envelope and tools/call params
   |-- Authenticate configured token -> role (admin/viewer)
   |-- tools/list: forward JSON body and downstream response unchanged
   |-- tools/call + name starts with "admin_" + role != admin:
   |      return HTTP 403 + JSON-RPC error (-32001), DO NOT forward
   |-- Other authenticated requests: forward to downstream
   v
Mock HTTP MCP Server (127.0.0.1:8001)
   |-- tools/list: get_status, admin_reset_key
   |-- tools/call: simulate calls to either tool
   `-- Track requests for integration testing
```

## Folder contents

| File | Purpose |
| --- | --- |
| `gateway.py` | Bearer-token authentication, JSON-RPC inspection, admin-tool authorization, upstream HTTP forwarding, error handling |
| `mock_mcp_server.py` | Standalone downstream HTTP/JSON-RPC test service |
| `test_gateway.py` | 22 automated end-to-end tests using real localhost HTTP sockets |
| `requirements.txt` | Documents that no third-party Python packages are required |
| `.env.example` | Example configuration variables (not automatically loaded) |
| `README.md` | Setup, usage, security decisions, test instructions |

## Quick start on macOS (Terminal)

Start **two terminals** in this directory. You do *not* need to run the Task 1 server for this task: Task 1 uses stdio, while Task 2 explicitly asks for an HTTP/JSON-RPC mock downstream.

Terminal A — downstream:

```bash
cd ~/Documents/quilrai-ai-solutions-assessment/task-02-mcp-security-gateway
python3 mock_mcp_server.py
```

Terminal B — gateway:

```bash
cd ~/Documents/quilrai-ai-solutions-assessment/task-02-mcp-security-gateway
python3 gateway.py
```

Expected log messages:

```text
Mock MCP server listening at http://127.0.0.1:8001/mcp
MCP Gateway listening at http://127.0.0.1:8000/mcp
```

Terminal C — exercise requests:

**1. Viewer lists tools (forwarded):**

```bash
curl -i -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-viewer-token' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expected: HTTP `200`, JSON-RPC `result.tools` includes both `get_status` and `admin_reset_key`. The gateway **does not filter the list**, as explicitly required by the prompt; it enforces authorization at call time.

**2. Viewer calls ordinary tool (forwarded):**

```bash
curl -i -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-viewer-token' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_status","arguments":{}}}'
```

Expected: HTTP `200` and an MCP tool result containing `status: healthy`.

**3. Viewer calls administrative tool (blocked locally at the gateway):**

```bash
curl -i -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-viewer-token' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected: HTTP `403` with JSON-RPC error (the original ID is preserved):

```json
{"jsonrpc":"2.0","id":3,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

**4. Admin calls administrative tool (forwarded):**

```bash
curl -i -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-admin-token' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected: HTTP `200`; tool result contains `simulated admin key reset`.

Stop either server with `Control+C`.

## Automated end-to-end tests (only one terminal required)

From this folder:

```bash
python3 test_gateway.py
```

This launches the gateway and mock downstream on **random available loopback ports**, tests both, and shuts them down. **Do not launch the servers separately for automated tests.** The 22 checks cover:

- Viewer/admin listing tools
- Viewer access to ordinary tools
- Admin access to `admin_` tools
- Viewer prohibition of `admin_` calls, with a **downstream request counter proving no forwarding**
- Missing/invalid Bearer tokens and fake role claims
- Malformed `tools/call` params, invalid JSON, invalid JSON-RPC IDs
- HTTP and JSON-RPC error code preservation
- Transparent original request-body forwarding, forwarding of MCP metadata, and removal of Authorization upstream
- Notification behavior (202 without response body; block unauthorized notification)
- Handling upstream HTTP errors and unreachable downstream servers
- Rejecting conflicting MCP headers and ambiguous JSON (duplicate keys, NaN)

Success summary:

```text
Ran 22 tests in ...s

OK
```

## Authorization and error design

| Condition | HTTP | JSON-RPC code | Forwarded? |
| --- | ---: | ---: | --- |
| Valid `tools/list` | upstream status (200) | upstream response | Yes |
| Valid non-admin `tools/call` | upstream status (200) | upstream response | Yes |
| Admin tool with admin token | upstream status (200) | upstream response | Yes |
| `admin_` call with viewer token | 403 | `-32001` | **No** |
| Missing/unknown token | 401 | `-32000` | **No** |
| JSON parse failure | 400 | `-32700` | **No** |
| Invalid JSON-RPC request | 400 | `-32600` | **No** |
| Invalid `tools/call` params | 400 | `-32602` | **No** |
| Downstream unreachable | 502 | `-32002` | Attempted |

Tokens are **verified against configured values**, not decoded as unsigned JWTs or accepted as strings like `role=admin`. The example values (`dev-admin-token`, `dev-viewer-token`) are **local demo credentials**, not secure production secrets. Optionally override them in the gateway process:

```bash
export ADMIN_TOKEN='your-own-long-random-admin-token'
export VIEWER_TOKEN='your-own-long-random-viewer-token'
python3 gateway.py
```

If overridden, use the same token values in curl requests.

## Security engineering notes

- Both services bind to `127.0.0.1` by default; no public-facing listener.
- The gateway validates JSON-RPC envelopes, rejects malformed tool names and ambiguous payloads, and checks MCP header/body consistency.
- Access control is performed **before the upstream HTTP request is constructed**.
- A viewer cannot escalate by supplying an unverified `role=admin` token or request header.
- The gateway forwards the original JSON body without rewriting it, and passes through downstream HTTP status and JSON body, while omitting the client's Authorization header from the upstream request.
- A timeout and request/response size limits are applied; transport failures become safe JSON-RPC errors.
- Logs do not include authentication tokens.

**Scope:** This is intentionally a *lightweight HTTP/JSON-RPC assessment proxy*, not a fully compliant production MCP Streamable HTTP implementation. It does not implement SSE streaming, MCP protocol-version negotiation, TLS, JWT signature/JWKS validation, per-user audit retention, or distributed rate limiting. Those would be appropriate production extensions. For a live deployment, use an identity provider or verified JWTs, TLS, origin validation, least-privilege tool policy, and robust audit trails. The mock downstream is not the stdio MCP server from Task 1.
