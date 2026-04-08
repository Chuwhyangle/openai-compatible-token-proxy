from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    local_day TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    request_id TEXT,
    api_key_fingerprint TEXT,
    endpoint TEXT NOT NULL,
    method TEXT NOT NULL,
    model TEXT,
    is_stream INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    request_bytes INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    raw_usage_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_events_local_day
    ON usage_events(local_day);

CREATE INDEX IF NOT EXISTS idx_usage_events_model
    ON usage_events(model);

CREATE INDEX IF NOT EXISTS idx_usage_events_ts_utc
    ON usage_events(ts_utc DESC);
"""


@dataclass(frozen=True)
class UsageRecord:
    ts_utc: str
    local_day: str
    timezone_name: str
    request_id: str | None
    api_key_fingerprint: str | None
    endpoint: str
    method: str
    model: str | None
    is_stream: bool
    status_code: int
    latency_ms: int
    request_bytes: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    raw_usage_json: dict[str, Any] | None


class UsageStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA_SQL)
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def insert_usage(self, record: UsageRecord) -> None:
        connection = self._require_connection()
        await connection.execute(
            """
            INSERT INTO usage_events (
                ts_utc,
                local_day,
                timezone_name,
                request_id,
                api_key_fingerprint,
                endpoint,
                method,
                model,
                is_stream,
                status_code,
                latency_ms,
                request_bytes,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_input_tokens,
                reasoning_tokens,
                raw_usage_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.ts_utc,
                record.local_day,
                record.timezone_name,
                record.request_id,
                record.api_key_fingerprint,
                record.endpoint,
                record.method,
                record.model,
                int(record.is_stream),
                record.status_code,
                record.latency_ms,
                record.request_bytes,
                record.input_tokens,
                record.output_tokens,
                record.total_tokens,
                record.cached_input_tokens,
                record.reasoning_tokens,
                json.dumps(record.raw_usage_json, ensure_ascii=True)
                if record.raw_usage_json is not None
                else None,
            ),
        )
        await connection.commit()

    async def daily_totals(self, days: int) -> list[dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT
                local_day,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(cached_input_tokens) AS cached_input_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens
            FROM usage_events
            GROUP BY local_day
            ORDER BY local_day DESC
            LIMIT ?
            """,
            (days,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def model_totals(self, *, start_day: date) -> list[dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT
                COALESCE(model, '(unknown)') AS model,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens
            FROM usage_events
            WHERE local_day >= ?
            GROUP BY COALESCE(model, '(unknown)')
            ORDER BY total_tokens DESC, requests DESC
            """,
            (start_day.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def recent_requests(self, limit: int) -> list[dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT
                ts_utc,
                local_day,
                endpoint,
                model,
                total_tokens,
                input_tokens,
                output_tokens,
                status_code,
                latency_ms,
                is_stream
            FROM usage_events
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database connection has not been initialized.")
        return self._connection
