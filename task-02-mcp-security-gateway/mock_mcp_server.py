"""Mock downstream MCP HTTP/JSON-RPC server for Task 2 (no SDK dependency)."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("mock-mcp")

TOOLS = [
    {"name": "get_status", "description": "Get demo service status",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "admin_reset_key", "description": "Simulate an administrative key reset",
     "inputSchema": {"type": "object", "properties": {}}},
]


def make_mock_handler() -> type[BaseHTTPRequestHandler]:
    class MockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            LOGGER.info("mock %s", fmt % args)

        def respond(self, status: int, payload: dict | None) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
            self.send_response(status)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.respond(404, {"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32601, "message": "Endpoint not found"}})
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            # Track every request received downstream; tests prove denied calls never arrive.
            with self.server.request_lock:
                self.server.received_requests.append({
                    "raw": raw,
                    "authorization": self.headers.get("Authorization"),
                    "mcp_method": self.headers.get("Mcp-Method"),
                    "accept": self.headers.get("Accept"),
                })
            try:
                request = json.loads(raw)
            except ValueError:
                self.respond(400, {"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "Parse error"}})
                return

            # Notifications have no response body.
            if "id" not in request:
                self.respond(202, None)
                return

            request_id = request.get("id")
            method = request.get("method")
            if method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = request.get("params", {}).get("name")
                if name == "get_status":
                    text = "status: healthy"
                elif name == "admin_reset_key":
                    text = "simulated admin key reset"
                else:
                    self.respond(404, {"jsonrpc": "2.0", "id": request_id,
                                       "error": {"code": -32602, "message": "Unknown tool"}})
                    return
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            else:
                self.respond(404, {"jsonrpc": "2.0", "id": request_id,
                                   "error": {"code": -32601, "message": "Method not found"}})
                return
            self.respond(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    return MockHandler


def create_mock_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_mock_handler())
    server.received_requests = []  # type: ignore[attr-defined]
    server.request_lock = threading.Lock()  # type: ignore[attr-defined]
    return server


def main() -> None:
    host = os.getenv("MOCK_HOST", "127.0.0.1")
    port = int(os.getenv("MOCK_PORT", "8001"))
    server = create_mock_server(host, port)
    LOGGER.info("Mock MCP server listening at http://%s:%d/mcp", host, port)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
