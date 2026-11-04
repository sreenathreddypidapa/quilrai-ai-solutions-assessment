"""Concurrency-safe on-disk SQLite sliding-window token ledger.

A request atomically reserves an upper token bound before upstream dispatch.
On provider success, its authoritative token usage replaces the reservation.
SQLite BEGIN IMMEDIATE serializes check+insert even across processes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import sqlite3
import time
import uuid


@dataclass(frozen=True)
class Reservation:
    event_id: str
    remaining: int


class LimitExceeded(Exception):
    def __init__(self, retry_after: int, remaining: int):
        super().__init__("token budget exhausted")
        self.retry_after = retry_after
        self.remaining = remaining


class SlidingWindowTokenLimiter:
    def __init__(self, db_path: str, limit: int = 50_000, window_seconds: float = 60.0):
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window must be positive")
        self.db_path = str(Path(db_path).resolve())
        self.limit = limit
        self.window_seconds = window_seconds
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            yield db
        finally:
            db.close()

    def _initialize(self):
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS token_events (
                        event_id TEXT PRIMARY KEY,
                        tenant_hash TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        tokens INTEGER NOT NULL CHECK(tokens >= 0)
                    )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tenant_time ON token_events(tenant_hash, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON token_events(created_at)")

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    async def reserve(self, api_key: str, tokens: int, *, now: float | None = None) -> Reservation:
        if type(tokens) is not int or tokens < 1:
            raise ValueError("tokens must be a positive integer")
        instant = time.time() if now is None else float(now)
        return await asyncio.to_thread(self._reserve_sync, self._hash_key(api_key), tokens, instant)

    def _reserve_sync(self, tenant: str, tokens: int, now: float) -> Reservation:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM token_events WHERE created_at <= ?", (now - self.window_seconds,))
                rows = db.execute(
                    "SELECT created_at, tokens FROM token_events WHERE tenant_hash = ? ORDER BY created_at, event_id",
                    (tenant,),
                ).fetchall()
                used = sum(row[1] for row in rows)
                remaining = max(0, self.limit - used)
                if used + tokens > self.limit:
                    needed = used + tokens - self.limit
                    freed = 0
                    retry_at = now + self.window_seconds
                    for timestamp, count in rows:
                        freed += count
                        if freed >= needed:
                            retry_at = timestamp + self.window_seconds
                            break
                    db.execute("ROLLBACK")
                    raise LimitExceeded(max(1, math.ceil(retry_at - now)), remaining)
                event_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO token_events(event_id, tenant_hash, created_at, tokens) VALUES (?, ?, ?, ?)",
                    (event_id, tenant, now, tokens),
                )
                db.execute("COMMIT")
                return Reservation(event_id, remaining - tokens)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    async def finalize(self, reservation: Reservation, actual_tokens: int) -> None:
        if type(actual_tokens) is not int or actual_tokens < 0:
            raise ValueError("actual_tokens must be a nonnegative integer")
        await asyncio.to_thread(self._finalize_sync, reservation.event_id, actual_tokens)

    def _finalize_sync(self, event_id: str, actual_tokens: int):
        with self._connection() as db:
            db.execute("UPDATE token_events SET tokens = ? WHERE event_id = ?", (actual_tokens, event_id))

    async def release(self, reservation: Reservation) -> None:
        await asyncio.to_thread(self._release_sync, reservation.event_id)

    def _release_sync(self, event_id: str):
        with self._connection() as db:
            db.execute("DELETE FROM token_events WHERE event_id = ?", (event_id,))

    async def usage(self, api_key: str, *, now: float | None = None) -> int:
        instant = time.time() if now is None else float(now)
        return await asyncio.to_thread(self._usage_sync, self._hash_key(api_key), instant)

    def _usage_sync(self, tenant: str, now: float) -> int:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM token_events WHERE created_at <= ?", (now - self.window_seconds,))
            (total,) = db.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM token_events WHERE tenant_hash = ? AND created_at > ?",
                (tenant, now - self.window_seconds),
            ).fetchone()
            db.execute("COMMIT")
            return total
