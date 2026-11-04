"""End-to-end HTTP tests for the MCP gateway and mock downstream server.

Run: python3 test_gateway.py
The tests use real loopback sockets and ephemeral ports. No installs needed.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gateway import make_gateway_handler
from mock_mcp_server import create_mock_server


ADMIN = "integration-test-admin-token"
VIEWER = "integration-test-viewer-token"


def start_http(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


def send_post(url: str, payload: dict | list | None,
              token: str | None = VIEWER, *, raw: bytes | None = None,
              extra_headers: dict | None = None) -> tuple[int, dict | None, bytes]:
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=3) as res:
            status, body = res.status, res.read()
    except HTTPError as exc:
        with exc:
            status, body = exc.code, exc.read()
    return status, json.loads(body) if body else None, body


class GatewayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mock = create_mock_server("127.0.0.1", 0)
        cls.mock_thread = start_http(cls.mock)
        downstream = f"http://127.0.0.1:{cls.mock.server_port}/mcp"
        cls.gateway = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_gateway_handler(downstream, ADMIN, VIEWER)
        )
        cls.gateway_thread = start_http(cls.gateway)
        cls.url = f"http://127.0.0.1:{cls.gateway.server_port}/mcp"

    @classmethod
    def tearDownClass(cls) -> None:
        for server, thread in [(cls.gateway, cls.gateway_thread),
                               (cls.mock, cls.mock_thread)]:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def count_downstream(self) -> int:
        with self.mock.request_lock:
            return len(self.mock.received_requests)

    def assert_not_forwarded(self, before: int) -> None:
        self.assertEqual(before, self.count_downstream(),
                         "An unauthorized or invalid request reached downstream")

    def test_01_viewer_lists_tools_transparently(self):
        payload = {"jsonrpc": "2.0", "id": "list-01", "method": "tools/list",
                   "params": {"cursor": "abc"}}
        raw = json.dumps(payload, separators=(", ", ": ")).encode()
        status, response, _ = send_post(
            self.url, None, raw=raw,
            extra_headers={"Mcp-Method": "tools/list", "MCP-Protocol-Version": "2025-06-18"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["id"], "list-01")
        self.assertEqual({tool["name"] for tool in response["result"]["tools"]},
                         {"get_status", "admin_reset_key"})
        with self.mock.request_lock:
            last = self.mock.received_requests[-1]
        self.assertEqual(last["raw"], raw, "Request JSON bytes must be unchanged")
        self.assertEqual(last["mcp_method"], "tools/list")
        self.assertIsNone(last["authorization"], "Gateway must not leak Bearer credentials")

    def test_02_admin_lists_tools(self):
        status, response, _ = send_post(
            self.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(len(response["result"]["tools"]), 2)

    def test_03_viewer_can_call_regular_tool(self):
        status, response, _ = send_post(self.url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_status", "arguments": {}}})
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["content"][0]["text"], "status: healthy")

    def test_04_admin_can_call_admin_tool(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {}}}, ADMIN)
        self.assertEqual(status, 200)
        self.assertIn("simulated", response["result"]["content"][0]["text"])
        self.assertEqual(self.count_downstream(), before + 1)

    def test_05_viewer_admin_tool_blocked_without_forwarding(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url, {
            "jsonrpc": "2.0", "id": "blocked-id", "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {}}})
        self.assertEqual(status, 403)
        self.assertEqual(response, {"jsonrpc": "2.0", "id": "blocked-id",
                                    "error": {"code": -32001, "message": "Unauthorized Tool Call"}})
        self.assert_not_forwarded(before)

    def test_06_admin_prefix_also_blocked(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "admin_delete_everything"}})
        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], -32001)
        self.assert_not_forwarded(before)

    def test_07_missing_token_rejected(self):
        before = self.count_downstream()
        status, response, _ = send_post(
            self.url, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, None)
        self.assertEqual(status, 401)
        self.assertEqual(response["error"]["code"], -32000)
        self.assert_not_forwarded(before)

    def test_08_invalid_token_rejected(self):
        before = self.count_downstream()
        status, response, _ = send_post(
            self.url, {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, "wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(response["error"]["code"], -32000)
        self.assert_not_forwarded(before)

    def test_09_role_spoofing_in_unverified_token_fails(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url, {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "admin_reset_key"}}, "role=admin")
        self.assertEqual(status, 401)
        self.assertEqual(response["error"]["code"], -32000)
        self.assert_not_forwarded(before)

    def test_10_missing_tools_call_params(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url,
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32602)
        self.assert_not_forwarded(before)

    def test_11_non_string_tool_name(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url,
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": ["admin_reset_key"]}})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32602)
        self.assert_not_forwarded(before)

    def test_12_bad_json_parse_error(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url, None, raw=b'{"jsonrpc":')
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])
        self.assert_not_forwarded(before)

    def test_13_invalid_json_rpc_object(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url,
            {"jsonrpc": "1.0", "id": 13, "method": "tools/list"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32600)
        self.assert_not_forwarded(before)

    def test_14_downstream_error_transparently_preserved(self):
        status, response, _ = send_post(self.url,
            {"jsonrpc": "2.0", "id": 14, "method": "method/not-supported"})
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], -32601)
        self.assertEqual(response["id"], 14)

    def test_15_notification_forwarded_without_response_body(self):
        status, response, body = send_post(self.url,
            {"jsonrpc": "2.0", "method": "notifications/test"})
        self.assertEqual(status, 202)
        self.assertIsNone(response)
        self.assertEqual(body, b"")

    def test_16_admin_notification_blocked_without_downstream(self):
        before = self.count_downstream()
        status, response, body = send_post(self.url,
            {"jsonrpc": "2.0", "method": "tools/call",
             "params": {"name": "admin_reset_key"}})
        self.assertEqual(status, 403)
        self.assertIsNone(response)
        self.assertEqual(body, b"")
        self.assert_not_forwarded(before)

    def test_17_unreachable_downstream_becomes_safe_error(self):
        temp = ThreadingHTTPServer(("127.0.0.1", 0),
            make_gateway_handler("http://127.0.0.1:1/mcp", ADMIN, VIEWER))
        thread = start_http(temp)
        try:
            url = f"http://127.0.0.1:{temp.server_port}/mcp"
            status, response, _ = send_post(url,
                {"jsonrpc": "2.0", "id": "upstream", "method": "tools/list"})
            self.assertEqual(status, 502)
            self.assertEqual(response["id"], "upstream")
            self.assertEqual(response["error"]["code"], -32002)
        finally:
            temp.shutdown()
            temp.server_close()
            thread.join(timeout=3)

    def test_18_boolean_json_rpc_id_rejected(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url,
            {"jsonrpc": "2.0", "id": True, "method": "tools/list"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32600)
        self.assert_not_forwarded(before)

    def test_19_header_method_mismatch_rejected(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url,
            {"jsonrpc": "2.0", "id": 19, "method": "tools/list"},
            extra_headers={"Mcp-Method": "tools/call"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32600)
        self.assert_not_forwarded(before)

    def test_20_header_name_spoof_rejected(self):
        before = self.count_downstream()
        status, response, _ = send_post(self.url,
            {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
             "params": {"name": "get_status"}},
            extra_headers={"Mcp-Name": "admin_reset_key"})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32602)
        self.assert_not_forwarded(before)

    def test_21_duplicate_json_field_rejected(self):
        before = self.count_downstream()
        raw = (b'{"jsonrpc":"2.0","id":21,"method":"tools/call",'
               b'"params":{"name":"get_status","name":"admin_reset_key"}}')
        status, response, _ = send_post(self.url, None, raw=raw)
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32700)
        self.assert_not_forwarded(before)

    def test_22_nonstandard_nan_rejected(self):
        before = self.count_downstream()
        raw = b'{"jsonrpc":"2.0","id":22,"method":"tools/list","params":{"cursor":NaN}}'
        status, response, _ = send_post(self.url, None, raw=raw)
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], -32700)
        self.assert_not_forwarded(before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
