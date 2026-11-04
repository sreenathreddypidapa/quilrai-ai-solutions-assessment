"""Run: python test_router.py (runs offline against local aiohttp TestServers)."""
from __future__ import annotations
import asyncio
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from gateway import Settings, create_app, authenticated_key, extract_usage, parse_request
from mock_providers import create_app as create_provider
from rate_limiter import SlidingWindowTokenLimiter, LimitExceeded

logging.disable(logging.INFO)  # Keep the test output focused on assertions.


class ValidationTests(unittest.TestCase):
    def test_authentication_requires_bearer(self):
        self.assertIsNone(authenticated_key(None, ("key",)))
        self.assertIsNone(authenticated_key("Basic key", ("key",)))
        self.assertIsNone(authenticated_key("Bearer unknown", ("key",)))
        self.assertEqual(authenticated_key("Bearer key", ("key",)), "key")

    def test_bad_request_types(self):
        for data in [[], {}, {"prompt": "", "max_tokens": 1},
                     {"prompt": "hi", "max_tokens": True},
                     {"prompt": "hi", "max_tokens": 0},
                     {"prompt": "hi", "max_tokens": 4097},
                     {"prompt": "hi", "max_tokens": "10"},
                     {"prompt": "hi", "max_tokens": 10, "other": 2}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_request(data)

    def test_valid_request(self):
        self.assertEqual(parse_request({"prompt": "hi", "max_tokens": 20}), ("hi", 20))

    def test_usage_validation(self):
        self.assertEqual(extract_usage({"usage": {"prompt_tokens": 4, "completion_tokens": 5,
                                                    "total_tokens": 9}}), 9)
        self.assertIsNone(extract_usage({"usage": {"prompt_tokens": 4, "completion_tokens": 5,
                                                       "total_tokens": 8}}))
        self.assertIsNone(extract_usage({"usage": {"prompt_tokens": 4, "completion_tokens": -2,
                                                       "total_tokens": 2}}))
        self.assertIsNone(extract_usage({"usage": {"total_tokens": 3}}))


class SQLiteLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "usage.sqlite3")
        self.limiter = SlidingWindowTokenLimiter(self.path, limit=100, window_seconds=60)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_window_reserves_and_blocks(self):
        await self.limiter.reserve("tenant-a", 60, now=1000)
        await self.limiter.reserve("tenant-a", 40, now=1000)
        with self.assertRaises(LimitExceeded):
            await self.limiter.reserve("tenant-a", 1, now=1001)

    async def test_tenant_isolation(self):
        await self.limiter.reserve("tenant-a", 100, now=1000)
        await self.limiter.reserve("tenant-b", 100, now=1000)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1001), 100)
        self.assertEqual(await self.limiter.usage("tenant-b", now=1001), 100)

    async def test_exact_window_boundary_eviction(self):
        await self.limiter.reserve("tenant-a", 100, now=1000.0)
        with self.assertRaises(LimitExceeded):
            await self.limiter.reserve("tenant-a", 1, now=1059.999)
        await self.limiter.reserve("tenant-a", 100, now=1060.0)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1060), 100)

    async def test_true_sliding_window(self):
        await self.limiter.reserve("tenant-a", 30, now=1000)
        await self.limiter.reserve("tenant-a", 70, now=1030)
        await self.limiter.reserve("tenant-a", 30, now=1060)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1060), 100)
        with self.assertRaises(LimitExceeded):
            await self.limiter.reserve("tenant-a", 31, now=1060)
        await self.limiter.reserve("tenant-a", 70, now=1090)

    async def test_retry_after_seconds(self):
        await self.limiter.reserve("tenant-a", 80, now=1000)
        await self.limiter.reserve("tenant-a", 20, now=1010)
        with self.assertRaises(LimitExceeded) as cm:
            await self.limiter.reserve("tenant-a", 70, now=1012)
        self.assertEqual(cm.exception.retry_after, 48)

    async def test_finalize_actual_tokens(self):
        res = await self.limiter.reserve("tenant-a", 90, now=1000)
        await self.limiter.finalize(res, 12)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1000), 12)
        await self.limiter.reserve("tenant-a", 88, now=1001)

    async def test_release_failure(self):
        res = await self.limiter.reserve("tenant-a", 100, now=1000)
        await self.limiter.release(res)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1001), 0)

    async def test_persists_across_instances(self):
        await self.limiter.reserve("tenant-a", 99, now=1000)
        another = SlidingWindowTokenLimiter(self.path, 100)
        with self.assertRaises(LimitExceeded):
            await another.reserve("tenant-a", 2, now=1001)

    async def test_atomic_under_concurrency(self):
        # Two independent limiter instances sharing the same SQLite file.
        other = SlidingWindowTokenLimiter(self.path, 100)
        async def try_reserve(i):
            try:
                await (self.limiter if i % 2 else other).reserve("tenant-a", 10, now=1000)
                return True
            except LimitExceeded:
                return False
        results = await asyncio.gather(*(try_reserve(i) for i in range(40)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1001), 100)

    async def test_no_partial_charge_after_rejection(self):
        await self.limiter.reserve("tenant-a", 100, now=1000)
        with self.assertRaises(LimitExceeded):
            await self.limiter.reserve("tenant-a", 1, now=1000)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1000), 100)

    async def test_hashes_tenant_key_on_disk(self):
        import sqlite3
        await self.limiter.reserve("my-plaintext-key", 10, now=1000)
        with sqlite3.connect(self.path) as db:
            (stored,) = db.execute("SELECT tenant_hash FROM token_events LIMIT 1").fetchone()
        self.assertNotIn("my-plaintext-key", stored)
        self.assertEqual(len(stored), 64)

    async def test_never_expires_early(self):
        await self.limiter.reserve("tenant-a", 90, now=1000)
        self.assertEqual(await self.limiter.usage("tenant-a", now=1059.999), 90)


class GatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.primary = TestServer(create_provider("primary"))
        self.backup = TestServer(create_provider("backup"))
        await self.primary.start_server()
        await self.backup.start_server()
        self.settings = Settings(
            db_path=str(Path(self.tmp.name) / "limiter.sqlite3"),
            primary_url=str(self.primary.make_url("/generate")),
            backup_url=str(self.backup.make_url("/generate")),
            primary_timeout_ms=80,
            backup_timeout_ms=120,
            tokens_per_minute=50000,
            window_seconds=60,
            api_keys=("key-a", "key-b"),
        )
        self.gateway = TestServer(create_app(self.settings))
        await self.gateway.start_server()
        self.http = ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.gateway.close()
        await self.backup.close()
        await self.primary.close()
        self.tmp.cleanup()

    async def req(self, prompt: str = "hello", max_tokens: int = 3,
                  api_key: str | None = "key-a"):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key is not None else {}
        async with self.http.post(
            self.gateway.make_url("/v1/completions"),
            headers=headers, json={"prompt": prompt, "max_tokens": max_tokens},
        ) as resp:
            return resp.status, await resp.json()

    async def provider_calls(self, provider: str) -> int:
        server = self.primary if provider == "primary" else self.backup
        async with self.http.get(server.make_url("/_metrics")) as resp:
            return (await resp.json())["requests_received"]

    async def test_primary_success(self):
        status, data = await self.req()
        self.assertEqual(status, 200)
        self.assertEqual(data["provider"], "primary")
        self.assertFalse(data["fallback_used"])
        self.assertEqual(await self.provider_calls("backup"), 0)

    async def test_primary_429_failover(self):
        status, data = await self.req("[primary:429] hello")
        self.assertEqual(status, 200)
        self.assertEqual(data["provider"], "backup")
        self.assertEqual(data["fallback_reason"], "primary_429")
        self.assertEqual(await self.provider_calls("primary"), 1)
        self.assertEqual(await self.provider_calls("backup"), 1)

    async def test_timeout_failover(self):
        prompt = "[primary:timeout] hello"
        status, data = await self.req(prompt)
        self.assertEqual(status, 200)
        self.assertEqual(data["provider"], "backup")
        self.assertEqual(data["fallback_reason"], "primary_timeout")
        # Primary execution may have spent tokens before timing out.
        # Preserve upper-bound reservation rather than undercounting it.
        self.assertEqual(await self.gateway.app["limiter"].usage("key-a"), len(prompt.encode()) + 3)

    async def test_primary_500_no_fallback_and_sanitized(self):
        status, data = await self.req("[primary:500] hello")
        self.assertEqual(status, 502)
        self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertNotIn("STACK TRACE", str(data))
        self.assertEqual(await self.provider_calls("backup"), 0)

    async def test_both_providers_unavailable_sanitized(self):
        status, data = await self.req("[primary:429] [backup:500]")
        self.assertEqual(status, 502)
        self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertNotIn("SENSITIVE", str(data))
        self.assertNotIn("Traceback", str(data))

    async def test_fallback_timeout_sanitized(self):
        status, data = await self.req("[primary:429] [backup:timeout]")
        self.assertEqual(status, 502)
        self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")

    async def test_invalid_json_request(self):
        async with self.http.post(self.gateway.make_url("/v1/completions"),
                                  headers={"Authorization": "Bearer key-a",
                                           "Content-Type": "application/json"},
                                  data=b'{oops') as resp:
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["error"]["code"], "INVALID_REQUEST")

    async def test_missing_token_rejected_before_provider(self):
        status, data = await self.req(api_key=None)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(await self.provider_calls("primary"), 0)

    async def test_invalid_token_rejected(self):
        status, data = await self.req(api_key="not-real")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    async def test_reconciliation(self):
        status, data = await self.req("hi", 200)
        self.assertEqual(status, 200)
        usage = await self.gateway.app["limiter"].usage("key-a")
        self.assertEqual(usage, data["usage"]["total_tokens"])
        self.assertLess(usage, 200)

    async def test_usage_missing_keeps_reservation(self):
        prompt = "[primary:badusage]"
        status, data = await self.req(prompt, 100)
        self.assertEqual(status, 200)
        self.assertIsNone(data["usage"])
        usage = await self.gateway.app["limiter"].usage("key-a")
        self.assertEqual(usage, len(prompt.encode()) + 100)

    async def test_tenant_rate_limit_skips_upstream(self):
        self.gateway.app["limiter"].limit = 10
        status, data = await self.req("hello world", 3)
        self.assertEqual(status, 429)
        self.assertEqual(data["error"]["code"], "TOKEN_RATE_LIMITED")
        self.assertGreaterEqual(data["error"]["retry_after_seconds"], 1)
        self.assertEqual(await self.provider_calls("primary"), 0)

    async def test_tenant_isolation_integration(self):
        self.gateway.app["limiter"].limit = 9
        a_status, _ = await self.req("hello", 3, "key-a")  # exactly 8 tokens reserved
        b_status, _ = await self.req("hello", 3, "key-b")
        self.assertEqual(a_status, 200)
        self.assertEqual(b_status, 200)

    async def test_error_shape_consistency(self):
        failures = [(None, "hello", 3, 401), ("bad", "hello", 3, 401),
                    ("key-a", "[primary:500]", 3, 502)]
        for key, prompt, max_tokens, expected in failures:
            status, body = await self.req(prompt, max_tokens, key)
            self.assertEqual(status, expected)
            self.assertEqual(set(body), {"error", "request_id"})
            self.assertEqual(set(body["error"]), {"code", "message"})

    async def test_malformed_provider_response(self):
        status, body = await self.req("[primary:invalid]")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "UPSTREAM_UNAVAILABLE")

    async def test_upstream_not_forwarded_auth(self):
        status, _ = await self.req("hello")
        self.assertEqual(status, 200)
        async with self.http.get(self.primary.make_url("/_metrics")) as resp:
            metrics = await resp.json()
        self.assertFalse(metrics["received_authorization"])

    async def test_default_primary_timeout_is_3000ms(self):
        self.assertEqual(Settings().primary_timeout_ms, 3000)

    async def test_real_three_second_timeout_then_backup(self):
        # Real 3.0 second deadline, not merely a shortened test timeout.
        settings = Settings(
            db_path=str(Path(self.tmp.name) / "three_second.sqlite3"),
            primary_url=str(self.primary.make_url("/generate")),
            backup_url=str(self.backup.make_url("/generate")),
            primary_timeout_ms=3000,
            backup_timeout_ms=1000,
            api_keys=("key-a",),
        )
        gateway = TestServer(create_app(settings))
        await gateway.start_server()
        try:
            started = time.monotonic()
            async with self.http.post(gateway.make_url("/v1/completions"),
                                      headers={"Authorization": "Bearer key-a"},
                                      json={"prompt": "[primary:timeout]", "max_tokens": 5}) as response:
                self.assertEqual(response.status, 200)
                body = await response.json()
            elapsed = time.monotonic() - started
            self.assertEqual(body["provider"], "backup")
            self.assertEqual(body["fallback_reason"], "primary_timeout")
            self.assertGreaterEqual(elapsed, 2.90)
            self.assertLess(elapsed, 3.80)
        finally:
            await gateway.close()


    async def test_usage_settlement_is_durable(self):
        status, data = await self.req("hello there", 22)
        self.assertEqual(status, 200)
        replica = SlidingWindowTokenLimiter(self.settings.db_path, self.settings.tokens_per_minute)
        self.assertEqual(await replica.usage("key-a"), data["usage"]["total_tokens"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
