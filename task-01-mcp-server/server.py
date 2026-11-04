"""Task 1: official MCP Python SDK v1, strict Pydantic validation, stdio transport."""

import asyncio
import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ErrorData,
    INVALID_PARAMS,
    ServerResult,
    TextContent,
    Tool,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
)

# stdout is *never* used for logging; MCP SDK owns it completely.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(levelname)s %(message)s",
)
logger = logging.getLogger("customer-mcp")
server = Server("customer-refund-server")


class CustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: StrictStr = Field(pattern=r"^CUST-[0-9]{5}$")


class RefundRequest(CustomerRequest):
    amount: float = Field(gt=0, allow_inf_nan=False, strict=True)
    reason: StrictStr = Field(min_length=10)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if len(value.strip()) < 10:
            raise ValueError("reason must contain at least 10 non-whitespace characters")
        return value


CUSTOMERS = {
    "CUST-00001": {"customer_id": "CUST-00001", "name": "Sample Customer", "status": "active"},
    "CUST-00002": {"customer_id": "CUST-00002", "name": "Second Customer", "status": "active"},
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_customer_record",
            description="Retrieve a demo customer record by a CUST-XXXXX identifier.",
            inputSchema=CustomerRequest.model_json_schema(),
        ),
        Tool(
            name="trigger_refund",
            description="Simulate a refund; does not charge or transfer money.",
            inputSchema=RefundRequest.model_json_schema(),
        ),
    ]


def tool_result(data: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, allow_nan=False))],
        isError=is_error,
    )


async def execute_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    schemas = {
        "get_customer_record": CustomerRequest,
        "trigger_refund": RefundRequest,
    }
    if name not in schemas:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown tool: {name}"))

    try:
        request = schemas[name].model_validate(arguments if arguments is not None else {})
    except ValidationError as exc:
        # A bad request is a *protocol* error, not a successful tool response.
        validation_details = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="Invalid tool arguments", data=validation_details)
        ) from exc

    customer = CUSTOMERS.get(request.customer_id)
    if customer is None:
        # Valid request, but a business-level failure the model should see.
        return tool_result({"ok": False, "error": "Customer not found"}, is_error=True)

    if name == "get_customer_record":
        return tool_result({"ok": True, "customer": customer})

    logger.info("Demo refund requested for customer %s", request.customer_id)
    return tool_result({
        "ok": True,
        "status": "simulated",
        "customer_id": request.customer_id,
        "amount": request.amount,
        "reason": request.reason,
    })


async def strict_call_tool_handler(request: CallToolRequest) -> ServerResult:
    """Keep MCP errors as JSON-RPC errors in MCP SDK 1.x.

    SDK 1.x's @server.call_tool() catches McpError and turns it into an
    isError=true tool result, losing the required JSON-RPC -32602 code.
    Registering the official SDK's low-level request handler directly avoids
    that wrapper while leaving transport and JSON-RPC dispatch to the SDK.
    """
    result = await execute_tool(request.params.name, request.params.arguments)
    return ServerResult(result)


server.request_handlers[CallToolRequest] = strict_call_tool_handler


async def main() -> None:
    logger.info("Starting MCP server (stdio)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
