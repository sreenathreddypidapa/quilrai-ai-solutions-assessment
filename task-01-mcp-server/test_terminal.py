"""Run: python test_terminal.py -- validates MCP's *raw* stdout JSON-RPC wire."""
import asyncio
import json
import sys


async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "server.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def receive(expected_id):
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=8)
            if not line:
                raise AssertionError("Server stdout closed before response")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"Non-JSON line leaked to stdout: {line!r}") from exc
            assert message.get("jsonrpc") == "2.0", message
            if message.get("id") == expected_id:
                return message

    try:
        await send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
            "protocolVersion":"2025-06-18", "capabilities":{},
            "clientInfo":{"name":"terminal-test","version":"1.0"}}})
        init = await receive(1)
        assert "result" in init, init
        await send({"jsonrpc":"2.0","method":"notifications/initialized"})
        print("PASS initialize")

        await send({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
        listing = await receive(2)
        names = {tool["name"] for tool in listing["result"]["tools"]}
        assert names == {"get_customer_record", "trigger_refund"}, names
        print("PASS tools/list: 2 tools")

        cases = [
            ("valid customer", "get_customer_record", {"customer_id":"CUST-00001"}, True),
            ("valid refund", "trigger_refund", {"customer_id":"CUST-00001", "amount":25.5, "reason":"Duplicate payment"}, True),
            ("bad ID", "get_customer_record", {"customer_id":"CUST-1234"}, False),
            ("extra field", "get_customer_record", {"customer_id":"CUST-00001", "admin":True}, False),
            ("zero amount", "trigger_refund", {"customer_id":"CUST-00001", "amount":0, "reason":"Duplicate payment"}, False),
            ("negative amount", "trigger_refund", {"customer_id":"CUST-00001", "amount":-1, "reason":"Duplicate payment"}, False),
            ("amount as string", "trigger_refund", {"customer_id":"CUST-00001", "amount":"25", "reason":"Duplicate payment"}, False),
            ("short reason", "trigger_refund", {"customer_id":"CUST-00001", "amount":25, "reason":"Too short"}, False),
            ("whitespace reason", "trigger_refund", {"customer_id":"CUST-00001", "amount":25, "reason":"          "}, False),
            ("missing reason", "trigger_refund", {"customer_id":"CUST-00001", "amount":25}, False),
            ("unknown tool", "no_such_tool", {}, False),
        ]
        for index, (label, name, args, valid) in enumerate(cases, start=3):
            await send({"jsonrpc":"2.0","id":index,"method":"tools/call","params":{"name":name,"arguments":args}})
            response = await receive(index)
            if valid:
                assert "result" in response and not response["result"].get("isError"), response
            else:
                assert response.get("error", {}).get("code") == -32602, response
                assert "result" not in response, response
            print(f"PASS {label}")

        print("\nALL TESTS PASSED: JSON-RPC 2.0, validation codes, and stdout isolation")
    finally:
        if proc.returncode is None:
            proc.terminate()
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            _, stderr = await proc.communicate()
        if stderr:
            print("(Server debug logs were on stderr, not stdout.)")


if __name__ == "__main__":
    asyncio.run(main())
