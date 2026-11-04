"""Task 2: HTTP/JSON-RPC MCP authorization reverse proxy (Python stdlib).

Run: python3 gateway.py
Local-demo tokens are deliberately NOT production credentials.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("mcp-gateway")

MAX_BODY_BYTES = 1_048_576
MAX_UPSTREAM_BYTES = 4 * MAX_BODY_BYTES
TIMEOUT_SECONDS = 5
TOKEN_PATTERN = re.compile(r"^Bearer ([^\s]+)$", flags=re.IGNORECASE)


def token_role(header: str | None, admin_token: str, viewer_token: str) -> str | None:
    """Map an exact Bearer token to a trusted role. Never trust user-supplied role text."""
    match = TOKEN_PATTERN.fullmatch(header.strip()) if header else None
    if not match:
        return None
    token = match.group(1)
    # Constant-time equality for the configured local-demo tokens.
    if hmac.compare_digest(token, admin_token):
        return "admin"
    if hmac.compare_digest(token, viewer_token):
        return "viewer"
    return None


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """Reject ambiguous JSON that different upstream parsers might interpret differently."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_non_json_constant(value: str) -> None:
    """Python's json parser otherwise accepts NaN and Infinity (invalid JSON)."""
    raise ValueError(f"non-standard JSON constant: {value}")


def rpc_error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def make_gateway_handler(downstream_url: str, admin_token: str,
                         viewer_token: str) -> type[BaseHTTPRequestHandler]:
    """Handler factory also makes real-network integration tests straightforward."""
    if not admin_token or not viewer_token or admin_token == viewer_token:
        raise ValueError("Admin/viewer tokens must be distinct nonempty strings")
    if not downstream_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("For this exercise the downstream must be on loopback")

    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            LOGGER.info("gateway %s", fmt % args)

        def send_bytes(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def send_error_rpc(self, status: int, request_id: object,
                           code: int, message: str, notification: bool = False) -> None:
            if notification:
                # JSON-RPC notifications must not produce a JSON-RPC response.
                self.send_bytes(status, b"")
                return
            body = json.dumps(rpc_error(request_id, code, message),
                              separators=(",", ":")).encode("utf-8")
            self.send_bytes(status, body)

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.send_error_rpc(404, None, -32601, "Endpoint not found")
                return

            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                self.send_error_rpc(415, None, -32600, "Content-Type must be application/json")
                return

            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error_rpc(411, None, -32600, "Content-Length required")
                return
            if length < 1 or length > MAX_BODY_BYTES:
                self.send_error_rpc(413, None, -32600, "Invalid request size")
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys,
                                     parse_constant=reject_non_json_constant)
            except (ValueError, UnicodeDecodeError):
                self.send_error_rpc(400, None, -32700, "Parse error")
                return

            if not isinstance(payload, dict):
                self.send_error_rpc(400, None, -32600, "Invalid Request: expected object")
                return

            request_id = payload.get("id")
            has_id = "id" in payload
            # A JSON-RPC ID is a string, integer, or null; bool is not an integer ID.
            if has_id and (isinstance(request_id, bool) or
                           (request_id is not None and not isinstance(request_id, (str, int)))):
                self.send_error_rpc(400, None, -32600, "Invalid Request: invalid id")
                return

            method = payload.get("method")
            if payload.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method:
                self.send_error_rpc(400, request_id if has_id else None,
                                    -32600, "Invalid Request")
                return

            # Reject mismatches that might let a downstream server interpret a
            # forwarded MCP method/name header differently from the JSON body.
            method_header = self.headers.get("Mcp-Method")
            if method_header is not None and method_header != method:
                self.send_error_rpc(400, request_id, -32600, "MCP method header mismatch",
                                    notification=not has_id)
                return
            name_header = self.headers.get("Mcp-Name")
            if method == "tools/call" and name_header is not None:
                params_for_header = payload.get("params")
                if (not isinstance(params_for_header, dict) or
                        params_for_header.get("name") != name_header):
                    self.send_error_rpc(400, request_id, -32602, "MCP name header mismatch",
                                        notification=not has_id)
                    return

            # Detect ill-formed params before the authorization decision.
            if method == "tools/call":
                params = payload.get("params")
                if not isinstance(params, dict) or not isinstance(params.get("name"), str) or not params["name"]:
                    self.send_error_rpc(400, request_id, -32602, "Invalid params: tool name required",
                                        notification=not has_id)
                    return

            role = token_role(self.headers.get("Authorization"), admin_token, viewer_token)
            if role is None:
                self.send_error_rpc(401, request_id, -32000, "Authentication Required",
                                    notification=not has_id)
                return

            if method == "tools/call" and payload["params"]["name"].startswith("admin_") and role != "admin":
                LOGGER.warning("Blocked unauthorized administrative tool call: %s",
                               payload["params"]["name"])
                self.send_error_rpc(403, request_id, -32001, "Unauthorized Tool Call",
                                    notification=not has_id)
                return

            # For tools/list and every other permitted method, pass the original
            # JSON bytes to downstream (not a re-serialized object).
            headers = {"Content-Type": content_type,
                       "Accept": self.headers.get("Accept", "application/json")}
            # Preserve relevant MCP wire metadata without leaking user credentials
            # or forwarding hop-by-hop/proxy-controlled headers.
            for key, value in self.headers.items():
                lower = key.lower()
                if lower in {"mcp-protocol-version", "mcp-session-id", "mcp-method",
                             "mcp-name", "last-event-id"} or lower.startswith("mcp-param-"):
                    headers[key] = value

            upstream_request = Request(downstream_url, data=raw,
                                       headers=headers, method="POST")
            try:
                with urlopen(upstream_request, timeout=TIMEOUT_SECONDS) as response:
                    result = response.read(MAX_UPSTREAM_BYTES + 1)
                    if len(result) > MAX_UPSTREAM_BYTES:
                        self.send_error_rpc(502, request_id, -32002, "Upstream response too large",
                                            notification=not has_id)
                        return
                    self.send_bytes(response.status, result,
                                    response.headers.get("Content-Type", "application/json"))
            except HTTPError as exc:
                # Preserve downstream JSON-RPC error responses and their HTTP status.
                with exc:
                    result = exc.read(MAX_UPSTREAM_BYTES + 1)
                    if len(result) > MAX_UPSTREAM_BYTES:
                        self.send_error_rpc(502, request_id, -32002, "Upstream response too large",
                                            notification=not has_id)
                        return
                    self.send_bytes(exc.code, result,
                                    exc.headers.get("Content-Type", "application/json"))
            except (URLError, TimeoutError, OSError) as exc:
                LOGGER.warning("Downstream request failed: %s", exc)
                self.send_error_rpc(502, request_id, -32002, "Downstream Unavailable",
                                    notification=not has_id)

    return GatewayHandler


def main() -> None:
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("GATEWAY_PORT", "8000"))
    downstream_url = os.getenv("DOWNSTREAM_URL", "http://127.0.0.1:8001/mcp")
    admin_token = os.getenv("ADMIN_TOKEN", "dev-admin-token")
    viewer_token = os.getenv("VIEWER_TOKEN", "dev-viewer-token")
    handler = make_gateway_handler(downstream_url, admin_token, viewer_token)
    server = ThreadingHTTPServer((host, port), handler)
    LOGGER.info("MCP Gateway listening at http://%s:%d/mcp", host, port)
    LOGGER.info("Downstream MCP server: %s", downstream_url)
    LOGGER.warning("Demo token mapping is not production authentication")
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
