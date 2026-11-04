"""Task 4: asynchronous LLM gateway, SQLite token budget, deterministic fallback.

Run: python gateway.py
Requires: aiohttp (requirements.txt)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hmac
import json
import logging
import os
from pathlib import Path
import uuid

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from rate_limiter import LimitExceeded, SlidingWindowTokenLimiter

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("llm-gateway")


@dataclass(frozen=True)
class Settings:
    db_path: str = "./runtime/usage.sqlite3"
    primary_url: str = "http://127.0.0.1:8201/generate"
    backup_url: str = "http://127.0.0.1:8202/generate"
    primary_timeout_ms: int = 3000
    backup_timeout_ms: int = 5000
    tokens_per_minute: int = 50_000
    window_seconds: float = 60.0
    api_keys: tuple[str, ...] = ("dev-tenant-a-key", "dev-tenant-b-key")

    @classmethod
    def from_env(cls) -> "Settings":
        keys = tuple(k.strip() for k in os.environ.get(
            "GATEWAY_API_KEYS", "dev-tenant-a-key,dev-tenant-b-key").split(",") if k.strip())
        if not keys:
            raise ValueError("GATEWAY_API_KEYS must contain at least one API key")
        return cls(
            db_path=os.environ.get("GATEWAY_DB", "./runtime/usage.sqlite3"),
            primary_url=os.environ.get("PRIMARY_URL", "http://127.0.0.1:8201/generate"),
            backup_url=os.environ.get("BACKUP_URL", "http://127.0.0.1:8202/generate"),
            primary_timeout_ms=int(os.environ.get("PRIMARY_TIMEOUT_MS", "3000")),
            backup_timeout_ms=int(os.environ.get("BACKUP_TIMEOUT_MS", "5000")),
            tokens_per_minute=int(os.environ.get("TOKENS_PER_MINUTE", "50000")),
            window_seconds=float(os.environ.get("WINDOW_SECONDS", "60")),
            api_keys=keys,
        )


class ProviderFailure(Exception):
    def __init__(self, reason: str, *, ambiguous: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.ambiguous = ambiguous


def error_response(status: int, code: str, message: str, request_id: str, **safe_fields):
    payload = {"error": {"code": code, "message": message}, "request_id": request_id}
    if safe_fields:
        payload["error"].update(safe_fields)
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def authenticated_key(header: str | None, configured: tuple[str, ...]) -> str | None:
    if not header:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    # Match every configured key, avoiding timing differences in the comparison.
    candidate = parts[1]
    matches = [hmac.compare_digest(candidate, known) for known in configured]
    if any(matches):
        return candidate
    return None


def parse_request(data) -> tuple[str, int]:
    if not isinstance(data, dict) or set(data) != {"prompt", "max_tokens"}:
        raise ValueError("Expected prompt and max_tokens")
    prompt, max_tokens = data["prompt"], data["max_tokens"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 500_000:
        raise ValueError("prompt must be a nonempty string of at most 500000 UTF-8 bytes")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
        raise ValueError("max_tokens must be an integer between 1 and 4096")
    return prompt, max_tokens


def extract_usage(data: dict) -> int | None:
    """Use provider-reported total tokens when trustworthy and complete.

    Unknown/inconsistent usages retain the conservative reservation.
    """
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    p, c, total = (usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if all(type(x) is int and x >= 0 for x in (p, c, total)) and p + c == total:
        return total
    return None


async def call_provider(session: ClientSession, url: str, prompt: str, max_tokens: int,
                        timeout_ms: int) -> dict:
    # Both network send and entire response body must finish inside this timeout.
    # Tenant Authorization is intentionally never forwarded upstream.
    try:
        async with asyncio.timeout(timeout_ms / 1000.0):
            async with session.post(
                url,
                json={"prompt": prompt, "max_tokens": max_tokens},
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status == 429:
                    raise ProviderFailure("rate_limited")
                if response.status != 200:
                    raise ProviderFailure("upstream_error")
                try:
                    result = await response.json(content_type="application/json")
                except (ValueError, ClientError):
                    raise ProviderFailure("invalid_provider_response") from None
                if not isinstance(result, dict) or not isinstance(result.get("output"), str):
                    raise ProviderFailure("invalid_provider_response")
                return result
    except TimeoutError:
        raise ProviderFailure("timeout", ambiguous=True) from None
    except (ClientError, OSError):
        raise ProviderFailure("connection_error", ambiguous=True) from None


async def completions(request: web.Request) -> web.Response:
    request_id = str(uuid.uuid4())
    settings: Settings = request.app["settings"]
    limiter: SlidingWindowTokenLimiter = request.app["limiter"]
    key = authenticated_key(request.headers.get("Authorization"), settings.api_keys)
    if key is None:
        return error_response(401, "UNAUTHORIZED", "Valid Bearer token required", request_id)

    try:
        data = await request.json()
        prompt, max_tokens = parse_request(data)
    except (ValueError, UnicodeError, json.JSONDecodeError, web.HTTPException):
        return error_response(400, "INVALID_REQUEST", "Invalid completion request", request_id)

    # A conservative ceiling for BPE-like tokenizers: a token consumes at
    # least one input byte. We reserve bytes(prompt) + maximum output tokens.
    # When a provider reports authoritative token usage, we settle to it.
    # This favors enforcing the quota over maximizing admission rates.
    reserved_tokens = len(prompt.encode("utf-8")) + max_tokens
    try:
        reservation = await limiter.reserve(key, reserved_tokens)
    except LimitExceeded as exc:
        return error_response(429, "TOKEN_RATE_LIMITED", "Per-tenant token budget exceeded", request_id,
                              retry_after_seconds=exc.retry_after)
    except Exception:
        LOG.exception("SQLite rate limit reservation failure (request_id=%s)", request_id)
        return error_response(503, "GATEWAY_UNAVAILABLE", "Gateway temporarily unavailable", request_id)

    release_if_failed = True
    # If any provider timed out, some tokens may have been spent upstream even if
    # this gateway fails. Preserve the reservation for the remainder of the minute.
    uncertain_spend = False
    try:
        session: ClientSession = request.app["http"]
        provider = "primary"
        fallback_reason = None
        try:
            result = await call_provider(session, settings.primary_url, prompt, max_tokens,
                                         settings.primary_timeout_ms)
        except ProviderFailure as exc:
            if exc.reason not in ("rate_limited", "timeout"):
                uncertain_spend = exc.ambiguous
                return error_response(502, "UPSTREAM_UNAVAILABLE", "Model provider unavailable", request_id)
            uncertain_spend = exc.ambiguous
            fallback_reason = "primary_429" if exc.reason == "rate_limited" else "primary_timeout"
            provider = "backup"
            try:
                result = await call_provider(session, settings.backup_url, prompt, max_tokens,
                                             settings.backup_timeout_ms)
            except ProviderFailure as backup_error:
                uncertain_spend = uncertain_spend or backup_error.ambiguous
                return error_response(502, "UPSTREAM_UNAVAILABLE", "Model providers unavailable", request_id)

        actual_usage = extract_usage(result)
        if actual_usage is not None and not uncertain_spend:
            await limiter.finalize(reservation, actual_usage)
        # If a primary timed out, it may have consumed unknown tokens. Keep
        # the conservative reservation even when the backup succeeds.
        # Missing/malformed usage also retains the original reservation.
        release_if_failed = False
        return web.json_response({
            "request_id": request_id,
            "provider": provider,
            "fallback_used": fallback_reason is not None,
            "fallback_reason": fallback_reason,
            "output": result["output"],
            "usage": result.get("usage") if actual_usage is not None else None,
        }, headers={"Cache-Control": "no-store"})
    except Exception:
        LOG.exception("Internal routing error (request_id=%s)", request_id)
        return error_response(503, "GATEWAY_UNAVAILABLE", "Gateway temporarily unavailable", request_id)
    finally:
        if release_if_failed and not uncertain_spend:
            try:
                await limiter.release(reservation)
            except Exception:
                LOG.exception("Reservation release failed (request_id=%s)", request_id)


async def health(_: web.Request):
    return web.json_response({"status": "ok"})


def create_app(settings: Settings | None = None) -> web.Application:
    settings = settings or Settings.from_env()
    if settings.primary_timeout_ms <= 0 or settings.backup_timeout_ms <= 0:
        raise ValueError("provider timeouts must be positive")
    app = web.Application(client_max_size=1024 ** 2)
    app["settings"] = settings
    app["limiter"] = SlidingWindowTokenLimiter(settings.db_path, settings.tokens_per_minute,
                                               settings.window_seconds)

    async def session_context(app):
        async with ClientSession(timeout=ClientTimeout(total=None)) as session:
            app["http"] = session
            yield

    app.cleanup_ctx.append(session_context)
    app.router.add_post("/v1/completions", completions)
    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    config = Settings.from_env()
    LOG.info("LLM router starting; SQLite=%s; token limit=%s per %ss; primary timeout=%sms",
             config.db_path, config.tokens_per_minute, config.window_seconds, config.primary_timeout_ms)
    if any(k.startswith("dev-") for k in config.api_keys):
        LOG.warning("DEMO API keys configured: never expose this service publicly with demo credentials")
    web.run_app(create_app(config), host="127.0.0.1", port=int(os.environ.get("GATEWAY_PORT", "8200")), print=None)
