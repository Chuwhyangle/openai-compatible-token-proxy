from __future__ import annotations

import html
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterable

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.config import settings
from app.database import UsageRecord, UsageStore
from app.usage import (
    StreamingUsageTap,
    decode_json_body,
    fingerprint_api_key,
    maybe_enable_chat_stream_usage,
    normalize_usage,
)


HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = UsageStore(settings.db_path)
    await store.initialize()

    timeout = httpx.Timeout(settings.request_timeout_s, connect=30.0)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)

    app.state.store = store
    app.state.client = client

    try:
        yield
    finally:
        await client.aclose()
        await store.close()


app = FastAPI(title="OpenAI-Compatible Token Proxy", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats/summary")
async def stats_summary(request: Request) -> dict[str, Any]:
    store: UsageStore = request.app.state.store
    daily_rows = await store.daily_totals(days=max(settings.dashboard_days, 7))
    return _build_summary_payload(daily_rows)


@app.get("/stats/daily")
async def stats_daily(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> list[dict[str, Any]]:
    store: UsageStore = request.app.state.store
    return await store.daily_totals(days=days)


@app.get("/stats/models")
async def stats_models(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
) -> list[dict[str, Any]]:
    store: UsageStore = request.app.state.store
    start_day = datetime.now(settings.timezone).date() - timedelta(days=days - 1)
    return await store.model_totals(start_day=start_day)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    store: UsageStore = request.app.state.store
    daily_rows = await store.daily_totals(days=settings.dashboard_days)
    model_rows = await store.model_totals(
        start_day=datetime.now(settings.timezone).date() - timedelta(days=6)
    )
    recent_rows = await store.recent_requests(limit=12)
    summary = _build_summary_payload(daily_rows)

    html_content = _render_dashboard(
        summary=summary,
        daily_rows=daily_rows,
        model_rows=model_rows,
        recent_rows=recent_rows,
    )
    return HTMLResponse(content=html_content)


@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_request(path: str, request: Request) -> Response:
    client: httpx.AsyncClient = request.app.state.client
    store: UsageStore = request.app.state.store

    start_time = perf_counter()
    body = await request.body()
    content_type = request.headers.get("content-type")
    patched_body, parsed_request = maybe_enable_chat_stream_usage(
        path=path,
        content_type=content_type,
        body=body,
        force_enabled=settings.force_chat_stream_usage,
    )
    if patched_body is not body:
        body = patched_body
    elif parsed_request is None:
        parsed_request = decode_json_body(content_type, body)

    is_stream = bool(parsed_request and parsed_request.get("stream"))
    authorization = request.headers.get("authorization")
    api_key_fingerprint = fingerprint_api_key(authorization)

    headers = _filter_request_headers(request.headers.items())
    if settings.upstream_api_key and "authorization" not in {
        key.lower() for key in headers
    }:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key}"
        api_key_fingerprint = fingerprint_api_key(headers["Authorization"])

    upstream_url = f"{settings.upstream_base_url}/v1/{path}"
    upstream_request = client.build_request(
        method=request.method,
        url=upstream_url,
        params=request.query_params,
        headers=headers,
        content=body,
    )

    try:
        upstream_response = await client.send(upstream_request, stream=is_stream)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc

    response_headers = _filter_response_headers(upstream_response.headers.items())
    upstream_content_type = upstream_response.headers.get("content-type", "")

    if is_stream or "text/event-stream" in upstream_content_type.lower():
        usage_tap = StreamingUsageTap()

        async def stream_bytes():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    usage_tap.feed(chunk)
                    yield chunk
            finally:
                usage_tap.finalize()
                await _store_usage_if_present(
                    store=store,
                    usage=usage_tap.usage,
                    path=path,
                    method=request.method,
                    api_key_fingerprint=api_key_fingerprint,
                    is_stream=True,
                    status_code=upstream_response.status_code,
                    latency_ms=int((perf_counter() - start_time) * 1000),
                    request_bytes=len(body),
                )
                await upstream_response.aclose()

        return StreamingResponse(
            stream_bytes(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_content_type or "text/event-stream",
        )

    response_body = await upstream_response.aread()
    await upstream_response.aclose()

    usage = None
    parsed_response = decode_json_body(upstream_content_type, response_body)
    if parsed_response is not None:
        usage = normalize_usage(parsed_response)

    await _store_usage_if_present(
        store=store,
        usage=usage,
        path=path,
        method=request.method,
        api_key_fingerprint=api_key_fingerprint,
        is_stream=False,
        status_code=upstream_response.status_code,
        latency_ms=int((perf_counter() - start_time) * 1000),
        request_bytes=len(body),
    )

    return Response(
        content=response_body,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_content_type or None,
    )


async def _store_usage_if_present(
    *,
    store: UsageStore,
    usage,
    path: str,
    method: str,
    api_key_fingerprint: str | None,
    is_stream: bool,
    status_code: int,
    latency_ms: int,
    request_bytes: int,
) -> None:
    if usage is None:
        return

    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(settings.timezone)
    record = UsageRecord(
        ts_utc=now_utc.isoformat(),
        local_day=local_now.date().isoformat(),
        timezone_name=settings.timezone_name,
        request_id=usage.request_id,
        api_key_fingerprint=api_key_fingerprint,
        endpoint=f"/v1/{path}",
        method=method,
        model=usage.model,
        is_stream=is_stream,
        status_code=status_code,
        latency_ms=latency_ms,
        request_bytes=request_bytes,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        raw_usage_json=usage.raw_usage,
    )
    await store.insert_usage(record)


def _filter_request_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _filter_response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _build_summary_payload(daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    local_today = datetime.now(settings.timezone).date()
    local_yesterday = local_today - timedelta(days=1)
    seven_days_ago = local_today - timedelta(days=6)

    rows_by_day = {row["local_day"]: row for row in daily_rows}

    today_row = rows_by_day.get(local_today.isoformat(), {})
    yesterday_row = rows_by_day.get(local_yesterday.isoformat(), {})

    trailing_7_total = 0
    trailing_7_requests = 0
    for row in daily_rows:
        row_day = datetime.fromisoformat(row["local_day"]).date()
        if row_day >= seven_days_ago:
            trailing_7_total += row["total_tokens"] or 0
            trailing_7_requests += row["requests"] or 0

    return {
        "timezone": settings.timezone_name,
        "today": {
            "date": local_today.isoformat(),
            "requests": today_row.get("requests", 0),
            "input_tokens": today_row.get("input_tokens", 0),
            "output_tokens": today_row.get("output_tokens", 0),
            "total_tokens": today_row.get("total_tokens", 0),
        },
        "yesterday": {
            "date": local_yesterday.isoformat(),
            "requests": yesterday_row.get("requests", 0),
            "total_tokens": yesterday_row.get("total_tokens", 0),
        },
        "last_7_days": {
            "requests": trailing_7_requests,
            "total_tokens": trailing_7_total,
        },
    }


def _render_dashboard(
    *,
    summary: dict[str, Any],
    daily_rows: list[dict[str, Any]],
    model_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
) -> str:
    daily_max = max((row["total_tokens"] or 0 for row in daily_rows), default=1)

    daily_table_rows = []
    for row in daily_rows:
        total_tokens = row["total_tokens"] or 0
        width = max(2, int((total_tokens / daily_max) * 100)) if daily_max else 0
        daily_table_rows.append(
            f"""
            <tr>
                <td>{html.escape(row["local_day"])}</td>
                <td>{row["requests"]}</td>
                <td>{row["input_tokens"] or 0:,}</td>
                <td>{row["output_tokens"] or 0:,}</td>
                <td>{total_tokens:,}</td>
                <td><div class="bar"><span style="width:{width}%"></span></div></td>
            </tr>
            """
        )

    model_table_rows = []
    for row in model_rows:
        model_table_rows.append(
            f"""
            <tr>
                <td>{html.escape(str(row["model"]))}</td>
                <td>{row["requests"]}</td>
                <td>{row["input_tokens"] or 0:,}</td>
                <td>{row["output_tokens"] or 0:,}</td>
                <td>{row["total_tokens"] or 0:,}</td>
            </tr>
            """
        )

    recent_table_rows = []
    for row in recent_rows:
        recent_table_rows.append(
            f"""
            <tr>
                <td>{html.escape(row["ts_utc"])}</td>
                <td>{html.escape(row["endpoint"])}</td>
                <td>{html.escape(str(row["model"] or "(unknown)"))}</td>
                <td>{row["total_tokens"] or 0:,}</td>
                <td>{row["latency_ms"]} ms</td>
                <td>{'yes' if row["is_stream"] else 'no'}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenAI-Compatible Token Proxy</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --card: #ffffff;
      --ink: #10223a;
      --muted: #5e6f85;
      --line: #d7e0eb;
      --accent: #1479ff;
    }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #edf4ff 0%, var(--bg) 100%);
      color: var(--ink);
      font-family: "Segoe UI", Arial, sans-serif;
    }}
    .wrap {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    p {{
      color: var(--muted);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin: 20px 0 28px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 10px 30px rgba(16, 34, 58, 0.06);
    }}
    .metric {{
      font-size: 2rem;
      font-weight: 700;
      margin-top: 8px;
    }}
    .label {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(16, 34, 58, 0.06);
    }}
    th, td {{
      padding: 12px 14px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
    }}
    th {{
      background: #f8fbff;
      font-size: 0.9rem;
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    .section {{
      margin-top: 28px;
    }}
    .bar {{
      width: 100%;
      min-width: 120px;
      height: 10px;
      background: #edf2f8;
      border-radius: 999px;
      overflow: hidden;
    }}
    .bar span {{
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--accent), #50a5ff);
      border-radius: 999px;
    }}
    .code {{
      display: inline-block;
      background: #eef4ff;
      color: #0f4db8;
      border-radius: 999px;
      padding: 6px 10px;
      font-family: Consolas, monospace;
      font-size: 0.9rem;
    }}
    @media (max-width: 700px) {{
      .wrap {{
        padding: 20px 14px 36px;
      }}
      th, td {{
        padding: 10px;
        font-size: 0.92rem;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>OpenAI-Compatible Token Proxy</h1>
    <p>
      Local usage tracker for requests sent through this proxy.
      Timezone: <span class="code">{html.escape(summary["timezone"])}</span>
    </p>

    <div class="grid">
      <div class="card">
        <div class="label">Today</div>
        <div class="metric">{summary["today"]["total_tokens"]:,}</div>
        <div class="label">{summary["today"]["requests"]} requests on {html.escape(summary["today"]["date"])}</div>
      </div>
      <div class="card">
        <div class="label">Today Input</div>
        <div class="metric">{summary["today"]["input_tokens"]:,}</div>
        <div class="label">Input tokens today</div>
      </div>
      <div class="card">
        <div class="label">Today Output</div>
        <div class="metric">{summary["today"]["output_tokens"]:,}</div>
        <div class="label">Output tokens today</div>
      </div>
      <div class="card">
        <div class="label">Last 7 Days</div>
        <div class="metric">{summary["last_7_days"]["total_tokens"]:,}</div>
        <div class="label">{summary["last_7_days"]["requests"]} requests</div>
      </div>
    </div>

    <div class="section">
      <h2>Daily Totals</h2>
      <table>
        <thead>
          <tr>
            <th>Day</th>
            <th>Requests</th>
            <th>Input</th>
            <th>Output</th>
            <th>Total</th>
            <th>Share</th>
          </tr>
        </thead>
        <tbody>
          {''.join(daily_table_rows) if daily_table_rows else '<tr><td colspan="6">No usage recorded yet.</td></tr>'}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Models (Last 7 Days)</h2>
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Requests</th>
            <th>Input</th>
            <th>Output</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {''.join(model_table_rows) if model_table_rows else '<tr><td colspan="5">No usage recorded yet.</td></tr>'}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Recent Requests</h2>
      <table>
        <thead>
          <tr>
            <th>Timestamp (UTC)</th>
            <th>Endpoint</th>
            <th>Model</th>
            <th>Total Tokens</th>
            <th>Latency</th>
            <th>Stream</th>
          </tr>
        </thead>
        <tbody>
          {''.join(recent_table_rows) if recent_table_rows else '<tr><td colspan="6">No requests logged yet.</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.listen_host,
        port=settings.listen_port,
        reload=False,
    )
