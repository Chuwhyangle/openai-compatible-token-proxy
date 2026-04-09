from __future__ import annotations

import asyncio
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
    upstream_name TEXT NOT NULL DEFAULT 'default',
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
"""


INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_usage_events_local_day
    ON usage_events(local_day);

CREATE INDEX IF NOT EXISTS idx_usage_events_model
    ON usage_events(model);

CREATE INDEX IF NOT EXISTS idx_usage_events_ts_utc
    ON usage_events(ts_utc DESC);

CREATE INDEX IF NOT EXISTS idx_usage_events_upstream_local_day
    ON usage_events(upstream_name, local_day);
"""


MIGRATION_COLUMNS = {
    "upstream_name": "ALTER TABLE usage_events ADD COLUMN upstream_name TEXT NOT NULL DEFAULT 'default'",
}


@dataclass(frozen=True)
class UsageRecord:
    ts_utc: str
    local_day: str
    timezone_name: str
    upstream_name: str
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
    def __init__(self, db_path: Path, *, write_batch_size: int = 50) -> None:
        self.db_path = db_path
        self.write_batch_size = write_batch_size
        self._connection: aiosqlite.Connection | None = None
        self._write_queue: asyncio.Queue[UsageRecord | None] | None = None
        self._writer_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA_SQL)
        await self._migrate_schema()
        await self._connection.executescript(INDEX_SQL)
        await self._connection.commit()

        self._write_queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def close(self) -> None:
        if self._connection is None:
            return

        await self.flush_pending()

        if self._write_queue is not None and self._writer_task is not None:
            await self._write_queue.put(None)
            await self._writer_task
            self._write_queue = None
            self._writer_task = None

        await self._connection.close()
        self._connection = None

    async def enqueue_usage(self, record: UsageRecord) -> None:
        if self._write_queue is None:
            raise RuntimeError("Write queue has not been initialized.")
        await self._write_queue.put(record)

    async def flush_pending(self) -> None:
        if self._write_queue is not None:
            await self._write_queue.join()

    async def daily_totals(
        self,
        *,
        days: int,
        upstream_name: str | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        where_sql, params = _build_filter_sql(upstream_name=upstream_name)
        cursor = await connection.execute(
            f"""
            SELECT
                local_day,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(cached_input_tokens) AS cached_input_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens
            FROM usage_events
            {where_sql}
            GROUP BY local_day
            ORDER BY local_day DESC
            LIMIT ?
            """,
            (*params, days),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def model_totals(
        self,
        *,
        start_day: date,
        upstream_name: str | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        clauses = ["local_day >= ?"]
        params: list[Any] = [start_day.isoformat()]
        if upstream_name is not None:
            clauses.append("upstream_name = ?")
            params.append(upstream_name)

        cursor = await connection.execute(
            f"""
            SELECT
                COALESCE(model, '(unknown)') AS model,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(cached_input_tokens) AS cached_input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens
            FROM usage_events
            WHERE {' AND '.join(clauses)}
            GROUP BY COALESCE(model, '(unknown)')
            ORDER BY total_tokens DESC, requests DESC
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def upstream_totals(self, *, start_day: date) -> list[dict[str, Any]]:
        connection = self._require_connection()
        cursor = await connection.execute(
            """
            SELECT
                upstream_name,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(cached_input_tokens) AS cached_input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens
            FROM usage_events
            WHERE local_day >= ?
            GROUP BY upstream_name
            ORDER BY total_tokens DESC, requests DESC, upstream_name ASC
            """,
            (start_day.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def recent_requests(
        self,
        *,
        limit: int,
        upstream_name: str | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        where_sql, params = _build_filter_sql(upstream_name=upstream_name)
        cursor = await connection.execute(
            f"""
            SELECT
                ts_utc,
                local_day,
                upstream_name,
                endpoint,
                model,
                total_tokens,
                input_tokens,
                cached_input_tokens,
                output_tokens,
                reasoning_tokens,
                status_code,
                latency_ms,
                is_stream
            FROM usage_events
            {where_sql}
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _migrate_schema(self) -> None:
        connection = self._require_connection()
        cursor = await connection.execute("PRAGMA table_info(usage_events)")
        rows = await cursor.fetchall()
        existing_columns = {row[1] for row in rows}
        for column_name, statement in MIGRATION_COLUMNS.items():
            if column_name not in existing_columns:
                await connection.execute(statement)

    async def _writer_loop(self) -> None:
        if self._write_queue is None:
            return

        while True:
            item = await self._write_queue.get()
            if item is None:
                self._write_queue.task_done()
                return

            batch = [item]
            while len(batch) < self.write_batch_size:
                try:
                    next_item = self._write_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_item is None:
                    self._write_queue.task_done()
                    await self._insert_batch(batch)
                    for _ in batch:
                        self._write_queue.task_done()
                    return
                batch.append(next_item)

            await self._insert_batch(batch)
            for _ in batch:
                self._write_queue.task_done()

    async def _insert_batch(self, records: list[UsageRecord]) -> None:
        connection = self._require_connection()
        await connection.executemany(
            """
            INSERT INTO usage_events (
                ts_utc,
                local_day,
                timezone_name,
                upstream_name,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.ts_utc,
                    record.local_day,
                    record.timezone_name,
                    record.upstream_name,
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
                )
                for record in records
            ],
        )
        await connection.commit()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database connection has not been initialized.")
        return self._connection


def _build_filter_sql(*, upstream_name: str | None) -> tuple[str, list[Any]]:
    if upstream_name is None:
        return "", []
    return "WHERE upstream_name = ?", [upstream_name]
