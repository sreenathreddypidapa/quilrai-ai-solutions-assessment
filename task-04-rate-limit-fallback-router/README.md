# Task 4 — SQLite Rate Limiting & Model Fallback Router

**QuilrAI AI Field Developer / Solutions Engineer Assessment**

A runnable Python 3.11+ LLM gateway that enforces a per-API-key rolling token limit in **on-disk SQLite** and routes synchronous text completions to a primary provider, with automatic failover to a backup only when the primary responds **HTTP 429** or exceeds **3,000 ms**. Both providers are local mocks so the assessment works without external API keys.

## Contents

| File | Purpose |
|---|---|
| `gateway.py` | Async HTTP API, Bearer authentication, primary/backup router, standardized errors |
| `rate_limiter.py` | SQLite-backed atomic sliding-window token ledger |
| `mock_providers.py` | Local primary/backup mock HTTP providers |
| `test_router.py` | Offline unit/integration suite, including an actual three-second timeout test |
| `requirements.txt` | `aiohttp` dependency |
| `.env.example` | Optional configuration reference; **do not use demo API keys in production** |
| `.gitignore` | Excludes local SQLite state, credentials, and Python cache |

## Architecture

```text
AI Client (POST /v1/completions, Bearer tenant key)
      |
      v
Gateway [127.0.0.1:8200]
      |-- validate & authenticate
      |-- SQLite BEGIN IMMEDIATE: rolling 60s reserve (default 50,000 tokens)
      |-- primary provider [127.0.0.1:8201] (3,000 ms whole-request deadline)
      |      |-- 200 -> return primary result
      |      |-- 429 or deadline -> backup provider [127.0.0.1:8202]
      |      `-- other failures -> sanitized 502 (no failover)
      |-- reconcile reserved tokens to upstream usage (if safe)
      `-- standardized public error response (never upstream stack trace)
```

## Install and test (Mac/Linux)

From your assessment project root:

```bash
cd task-04-rate-limit-fallback-router
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python test_router.py
```

Expected ending (35 tests as packaged):

```text
Ran 35 tests in ...

OK
```

The test script starts and stops temporary local mock/provider/gateway servers automatically and uses temporary SQLite databases. A real primary timeout test runs for approximately three seconds. No manual servers, cloud credentials, or network calls are required beyond local loopback.

## Run all three services (three Terminals)

**Terminal 1 — primary model**

```bash
cd ~/Documents/quilrai-ai-solutions-assessment/task-04-rate-limit-fallback-router
source .venv/bin/activate
PROVIDER=primary python mock_providers.py
```

**Terminal 2 — backup model**

```bash
cd ~/Documents/quilrai-ai-solutions-assessment/task-04-rate-limit-fallback-router
source .venv/bin/activate
PROVIDER=backup python mock_providers.py
```

**Terminal 3 — gateway**

```bash
cd ~/Documents/quilrai-ai-solutions-assessment/task-04-rate-limit-fallback-router
source .venv/bin/activate
python gateway.py
```

They listen on `127.0.0.1:8201`, `:8202`, and `:8200` respectively. Logs are written to the corresponding Terminal. Stop each process with **Ctrl+C**.

### Test from Terminal 4

**Normal success through primary:**

```bash
curl -sS http://127.0.0.1:8200/v1/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-tenant-a-key' \
  -d '{"prompt":"Hello from the gateway","max_tokens":20}'
```

Expected fields: `provider: "primary"`, `fallback_used: false`, `output`, `usage`.

**Simulate primary HTTP 429 → backup:**

```bash
curl -sS http://127.0.0.1:8200/v1/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-tenant-a-key' \
  -d '{"prompt":"[primary:429] Hello","max_tokens":20}'
```

Expected fields: `provider: "backup"`, `fallback_used: true`, `fallback_reason: "primary_429"`.

**Simulate primary 3,000 ms timeout → backup:**

```bash
curl -sS http://127.0.0.1:8200/v1/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-tenant-b-key' \
  -d '{"prompt":"[primary:timeout] Hello","max_tokens":20}'
```

Expected fields: `provider: "backup"`, `fallback_reason: "primary_timeout"`. The primary mock intentionally sleeps 3.3 seconds; the gateway deadline is 3.0 seconds.

**Both providers fail → sanitized error:**

```bash
curl -i http://127.0.0.1:8200/v1/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-tenant-a-key' \
  -d '{"prompt":"[primary:429] [backup:500]","max_tokens":20}'
```

Expected HTTP 502 and no raw error body or internal stack trace:

```json
{
  "error": {"code": "UPSTREAM_UNAVAILABLE", "message": "Model providers unavailable"},
  "request_id": "<generated-uuid>"
}
```

**Demonstrate rate-limit 429 without generating 50,000 tokens:** Stop Terminal 3, restart it with a smaller **test-only** quota and an independent database:

```bash
TOKENS_PER_MINUTE=50 GATEWAY_DB=./runtime/demo_limit.sqlite3 python gateway.py
```

Now submit a request reserving more than 50 tokens:

```bash
curl -i http://127.0.0.1:8200/v1/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-tenant-a-key' \
  -d '{"prompt":"hello","max_tokens":46}'
```

Five prompt bytes + 46 potential output tokens = 51 tokens, exceeding the quota; expect HTTP 429 with `TOKEN_RATE_LIMITED` and `retry_after_seconds`. The limiter rejects the request **before** calling either provider. To reset for normal demos, stop Terminal 3 and run `python gateway.py` without the extra environment variables.

## Gateway API

`POST /v1/completions`

Request:

```http
Authorization: Bearer <tenant-api-key>
Content-Type: application/json
```

```json
{"prompt": "Explain zero-trust networking briefly.", "max_tokens": 120}
```

Success (provider output is mock data):

```json
{
  "request_id": "<generated-uuid>",
  "provider": "primary",
  "fallback_used": false,
  "fallback_reason": null,
  "output": "primary generated: Explain zero-trust networking briefly.",
  "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8}
}
```

### Error contract

**Every application error** returned by the `/v1/completions` handler has this sanitized shape:

```json
{"error": {"code": "ERROR_CODE", "message": "Safe human-readable message"}, "request_id": "<uuid>"}
```

| HTTP | Code | Condition |
|---|---|---|
| 400 | `INVALID_REQUEST` | Malformed body / invalid fields |
| 401 | `UNAUTHORIZED` | Missing/unrecognized Bearer token |
| 429 | `TOKEN_RATE_LIMITED` | Exceeds tenant rolling quota (also includes `retry_after_seconds`) |
| 502 | `UPSTREAM_UNAVAILABLE` | Upstream failure or both providers unavailable |
| 503 | `GATEWAY_UNAVAILABLE` | Local gateway / database failure |

Gateway application responses contain **no raw upstream response text, exception class names, provider URLs, or stack traces**. Internal failures may be logged locally for operators, but request bodies and API keys are never logged. Invalid requests do not reach a provider.

## SQLite design and concurrency

- The persistent database defaults to `./runtime/usage.sqlite3` and is created automatically.
- `token_events(event_id, tenant_hash, created_at, tokens)` stores SHA-256 hashes of tenant API keys, not plaintext API keys.
- `BEGIN IMMEDIATE` wraps *evict expired rows → inspect current usage → compare against quota → insert new reservation* as **one atomic write transaction**. SQLite WAL allows concurrent reads and serialized writes, including independent processes using the same database file.
- Expire entries when `created_at <= now - 60 seconds`. Entries at 59.999 seconds count; entries at exactly 60 seconds do not.
- Each request reserves `(UTF-8 byte count of prompt) + max_tokens` *before* any provider request, so concurrent requests cannot oversubscribe token budgets through request races.
- On a confirmed success with valid `prompt_tokens`, `completion_tokens`, and matching `total_tokens` from the provider, settle the reservation to **actual reported token usage**. Inconsistent or missing provider usage leaves the conservative reservation in place.
- On a primary timeout, the primary may have processed an unknown number of tokens. In that case the reservation is retained even if fallback succeeds. This avoids treating potentially consumed primary tokens as zero; the accounting is conservative.
- A clearly unsuccessful provider call with no ambiguity releases the reservation. On ambiguous failures/timeouts, retain it for the rolling window to avoid undercounting.

**Trade-offs:** This uses a conservative byte-based upper bound for admission rather than an exact model tokenizer, which may reject some requests that an exact tokenizer would admit. Successful provider-reported usage is token-accurate to the extent that provider metadata is trustworthy. For production, use provider-specific tokenizer estimation, reconcile billed usage across providers, add robust identity management, distributed telemetry, rate-limit tiers, and monitoring. SQLite is suitable for a lightweight single-host deployment; use a shared transactional backend for multiple machines.

## Fallback and cancellation behavior

- Primary is attempted first for every accepted request.
- Fallback is **only** triggered by primary HTTP 429 or timeout after 3,000 ms, exactly as the challenge requires.
- `asyncio.timeout` bounds the *entire* primary HTTP operation, including reading the body, not just establishing a connection.
- Failed primary calls do not forward raw failure contents to the client.
- Backup has its own configurable timeout (default 5,000 ms).
- A primary HTTP 500 or connection failure gives a sanitized 502 without fallback, following the stated 429/timeout-only fallback policy.
- Timeout cancellation stops waiting for the primary, but does not guarantee a remote provider stops computing; the conservative ledger reservation addresses that uncertainty.

## Test coverage

`python test_router.py` includes 35 tests across:

- Input and authentication validation.
- Token usage parsing and error sanitization.
- Exact sliding-window boundary eviction and retry-after calculation.
- Per-key isolation; SQLite on-disk persistence across limiter instances.
- Real concurrency: simultaneous reservations from **two SQLite limiter objects** against a shared database.
- Provider-reported token reconciliation and missing-usage handling.
- HTTP 429 → fallback; real 3,000 ms timeout → fallback; primary errors without fallback.
- No API key forwarded upstream; denied calls skip downstream requests.
- Backup failures, malformed provider data, and standardized error envelopes.

## Configuration

Optional environment variables (see `.env.example`):

| Variable | Default |
|---|---|
| `GATEWAY_PORT` | `8200` |
| `GATEWAY_DB` | `./runtime/usage.sqlite3` |
| `GATEWAY_API_KEYS` | `dev-tenant-a-key,dev-tenant-b-key` (local demos only) |
| `PRIMARY_URL` | `http://127.0.0.1:8201/generate` |
| `BACKUP_URL` | `http://127.0.0.1:8202/generate` |
| `PRIMARY_TIMEOUT_MS` | `3000` |
| `BACKUP_TIMEOUT_MS` | `5000` |
| `TOKENS_PER_MINUTE` | `50000` |
| `WINDOW_SECONDS` | `60` |

Security: Both gateway and mock providers bind to loopback interfaces by default. Configure real API keys in environment variables (never in committed code). API key comparisons use constant-time string comparison and the ledger stores only hashes. The included keys and mocks are deliberately **not production authentication or provider integration**.
