# Task 3 — LLM Gateway Streaming Guardrail

Python asyncio/aiohttp HTTP gateway that consumes OpenAI-style SSE `choices[0].delta.content`, redacts email addresses, SSNs, and Luhn-valid credit card numbers across arbitrarily split deltas, and streams sanitized SSE to the caller. Mock provider echoes the prompt in 3-character deltas. No real LLM account needed.

## Run (two terminals)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python provider_mock.py
```

In a second terminal (activate the same virtualenv):

```bash
source .venv/bin/activate
python gateway.py
```

In a third terminal:

```bash
curl -N http://127.0.0.1:8100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Hello jane@example.com, SSN 123-45-6789, card 4111-1111-1111-1111!"}'
```

The streamed deltas contain `[REDACTED]` instead of those sensitive values. The endpoint uses the same SSE delta shape as the mock provider.

## Tests

```bash
python test_guardrail.py
```

## Design

- **Bounded memory:** the scanner retains only a current candidate token (max 320 characters) and one bounded SSE line (max 64 KiB), never the entire model response. Oversized tokens fail closed as `[REDACTED]`.
- **Split-delta safety:** PII tokens are held until a delimiter or end of stream; no sensitive token prefix is emitted before classification.
- **Latency:** plain text is emitted at token boundaries, and no full-response buffering occurs. A long uninterrupted token is delayed for safety. `X-Accel-Buffering: no` and `Cache-Control: no-transform` discourage intermediary buffering.
- **SSN:** simple 3-2-4 US pattern; does not verify issuance.
- **Credit cards:** 13–19 digits with optional spaces/hyphens and a valid Luhn checksum; Luhn is not proof of a real card.
- **Demo only:** provider is a deterministic SSE mock; set `PROVIDER_URL` to an upstream compatible endpoint to adapt the transport. Production would also need provider credentials, TLS, access controls, retry/cancellation policies, stronger PII detection, observability, and provider-specific SSE handling.

## Limitations

Regex redaction cannot guarantee capture of all PII formats or Unicode/obfuscated variants. The candidate character class is intentionally conservative and may delay certain non-PII tokens. An upstream SSE protocol error after headers are sent is reported as an `event: error` message. This is an HTTP/SSE demo, not a full MCP transport implementation.
