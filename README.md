# QuilrAI — AI Field Developer / Solutions Engineer Assessment

This repository contains four technical assessment solutions
focused on MCP infrastructure, security, LLM streaming,
and resilient model routing.

## Implemented Tasks

| Task | Description | Tests |
|------|-------------|-------|
| 1 | Custom MCP Server with Strict Validation | 13 Passed |
| 2 | MCP Security Gateway Proxy | 22 Passed |
| 3 | LLM Streaming PII Guardrail | 13 Passed |
| 4 | SQLite Rate Limiter & Model Fallback Router | 35 Passed |

**Total: 83 automated tests passed.**

## Project Structure

- task-01-mcp-server/
- task-02-mcp-security-gateway/
- task-03-llm-streaming-guardrail/
- task-04-rate-limit-fallback-router/

Each task contains its own implementation, dependencies,
documentation, and automated tests.

## Running Tests

Create a Python virtual environment and install the
requirements in each task directory.

Task 1:
python test_terminal.py

Task 2:
python test_gateway.py

Task 3:
python test_guardrail.py

Task 4:
python test_router.py

## Technical Highlights

- Official MCP Python SDK
- Strict Pydantic validation and JSON-RPC handling
- HTTP reverse proxy and tool-level authorization
- Asynchronous SSE streaming with incremental PII redaction
- SQLite-backed sliding-window token rate limiting
- Async primary/backup LLM model routing
- Standardized error handling and sanitization

## Security and Limitations

All providers and sample credentials are mock/demo
implementations intended for local assessment testing.

No real refunds, payments, or external LLM API calls
are performed.

Refer to each task's README for detailed architecture,
instructions, tests, and limitations.
