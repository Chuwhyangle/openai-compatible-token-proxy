from __future__ import annotations

import html
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterable

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.config import UpstreamConfig, settings
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
    store = UsageStore(settings.db_path, write_batch_size=settings.write_batch_size)
    await store.initialize()

    timeout = httpx.Timeout(settings.request_timeout_s, connect=30.0)
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)
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
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "default_upstream": settings.default_upstream_name,
        "configured_upstreams": list(settings.upstreams),
    }


@app.get("/upstreams")
async def upstreams(request: Request) -> list[dict[str, Any]]:
    origin = _request_origin(request)
    return [
        {
            "name": upstream.name,
            "label": upstream.label,
            "is_default": upstream.name == settings.default_upstream_name,
            "local_base_url": _local_base_url(upstream.name, origin=origin),
        }
        for upstream in settings.upstreams.values()
    ]


@app.get("/stats/summary")
async def stats_summary(
    request: Request,
    upstream: str | None = Query(default=None),
) -> dict[str, Any]:
    store: UsageStore = request.app.state.store
    selected_upstream = _resolve_optional_upstream(upstream)
    await store.flush_pending()

    daily_rows = _enrich_usage_rows(await store.daily_totals(
        days=max(settings.dashboard_days, 7),
        upstream_name=selected_upstream,
    ))
    summary = _build_summary_payload(daily_rows)
    summary["selected_upstream"] = selected_upstream
    summary["selected_label"] = _selected_upstream_label(selected_upstream)
    return summary


@app.get("/stats/daily")
async def stats_daily(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    upstream: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    store: UsageStore = request.app.state.store
    selected_upstream = _resolve_optional_upstream(upstream)
    await store.flush_pending()
    return _enrich_usage_rows(
        await store.daily_totals(days=days, upstream_name=selected_upstream)
    )


@app.get("/stats/models")
async def stats_models(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    upstream: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    store: UsageStore = request.app.state.store
    selected_upstream = _resolve_optional_upstream(upstream)
    await store.flush_pending()
    start_day = datetime.now(settings.timezone).date() - timedelta(days=days - 1)
    return _enrich_usage_rows(
        await store.model_totals(start_day=start_day, upstream_name=selected_upstream)
    )


@app.get("/stats/upstreams")
async def stats_upstreams(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
) -> list[dict[str, Any]]:
    store: UsageStore = request.app.state.store
    await store.flush_pending()
    start_day = datetime.now(settings.timezone).date() - timedelta(days=days - 1)
    rows = _enrich_usage_rows(await store.upstream_totals(start_day=start_day))
    origin = _request_origin(request)
    return [_decorate_upstream_row(row, origin=origin) for row in rows]


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    upstream: str | None = Query(default=None),
) -> HTMLResponse:
    store: UsageStore = request.app.state.store
    selected_upstream = _resolve_optional_upstream(upstream)
    await store.flush_pending()

    today = datetime.now(settings.timezone).date()
    daily_rows = _enrich_usage_rows(await store.daily_totals(
        days=settings.dashboard_days,
        upstream_name=selected_upstream,
    ))
    model_rows = _enrich_usage_rows(await store.model_totals(
        start_day=today - timedelta(days=6),
        upstream_name=selected_upstream,
    ))
    recent_rows = _enrich_usage_rows(
        await store.recent_requests(limit=12, upstream_name=selected_upstream)
    )
    upstream_rows = _enrich_usage_rows(
        await store.upstream_totals(start_day=today - timedelta(days=6))
    )
    summary = _build_summary_payload(daily_rows)
    origin = _request_origin(request)

    html_content = _render_dashboard(
        summary=summary,
        daily_rows=daily_rows,
        model_rows=model_rows,
        recent_rows=recent_rows,
        upstream_rows=[_decorate_upstream_row(row, origin=origin) for row in upstream_rows],
        selected_upstream=selected_upstream,
        origin=origin,
    )
    return HTMLResponse(content=html_content)


@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_request_default(path: str, request: Request) -> Response:
    return await _proxy_request_for_upstream(
        request=request,
        path=path,
        upstream=_resolve_upstream(settings.default_upstream_name),
    )


@app.api_route(
    "/proxy/{upstream_name}/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_request_named(upstream_name: str, path: str, request: Request) -> Response:
    return await _proxy_request_for_upstream(
        request=request,
        path=path,
        upstream=_resolve_upstream(upstream_name),
    )


async def _proxy_request_for_upstream(
    *,
    request: Request,
    path: str,
    upstream: UpstreamConfig,
) -> Response:
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
    if upstream.api_key and "authorization" not in {key.lower() for key in headers}:
        headers["Authorization"] = f"Bearer {upstream.api_key}"
        api_key_fingerprint = fingerprint_api_key(headers["Authorization"])

    upstream_url = f"{upstream.base_url}/v1/{path}"
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
        raise HTTPException(
            status_code=502,
            detail=f"Upstream request to '{upstream.name}' failed: {exc}",
        ) from exc

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
                    upstream_name=upstream.name,
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
        upstream_name=upstream.name,
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
    upstream_name: str,
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
        upstream_name=upstream_name,
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
    await store.enqueue_usage(record)


def _resolve_upstream(upstream_name: str) -> UpstreamConfig:
    upstream = settings.upstreams.get(upstream_name)
    if upstream is None:
        raise HTTPException(status_code=404, detail=f"Unknown upstream '{upstream_name}'.")
    return upstream


def _resolve_optional_upstream(upstream_name: str | None) -> str | None:
    if upstream_name is None:
        return None
    normalized = upstream_name.strip()
    if normalized == "" or normalized.lower() == "all":
        return None
    return normalized


def _selected_upstream_label(upstream_name: str | None) -> str:
    if upstream_name is None:
        return "All APIs"
    return _upstream_meta(upstream_name)["label"]


def _request_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _local_base_url(upstream_name: str, *, origin: str | None = None) -> str:
    base_origin = origin or f"http://{settings.listen_host}:{settings.listen_port}"
    if upstream_name == settings.default_upstream_name:
        return f"{base_origin}/v1"
    return f"{base_origin}/proxy/{upstream_name}/v1"


def _upstream_meta(upstream_name: str, *, origin: str | None = None) -> dict[str, Any]:
    upstream = settings.upstreams.get(upstream_name)
    is_configured = upstream is not None
    label = upstream.label if upstream is not None else f"{upstream_name} (historical)"
    return {
        "name": upstream_name,
        "label": label,
        "is_default": upstream_name == settings.default_upstream_name,
        "is_configured": is_configured,
        "local_base_url": _local_base_url(upstream_name, origin=origin),
    }


def _decorate_upstream_row(row: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
    upstream_name = row["upstream_name"]
    decorated = dict(row)
    decorated.update(_upstream_meta(upstream_name, origin=origin))
    return decorated


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


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _enrich_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_enrich_usage_row(row) for row in rows]


def _enrich_usage_row(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    requests = _safe_int(enriched.get("requests"))
    input_tokens = _safe_int(enriched.get("input_tokens"))
    cached_input_tokens = _safe_int(enriched.get("cached_input_tokens"))
    output_tokens = _safe_int(enriched.get("output_tokens"))
    total_tokens = _safe_int(enriched.get("total_tokens"))
    reasoning_tokens = _safe_int(enriched.get("reasoning_tokens"))

    cached_input_tokens = min(max(cached_input_tokens, 0), max(input_tokens, 0))
    fresh_input_tokens = max(input_tokens - cached_input_tokens, 0)
    fresh_total_tokens = fresh_input_tokens + output_tokens
    cache_ratio = (cached_input_tokens / input_tokens) if input_tokens else 0.0

    enriched["requests"] = requests
    enriched["input_tokens"] = input_tokens
    enriched["cached_input_tokens"] = cached_input_tokens
    enriched["fresh_input_tokens"] = fresh_input_tokens
    enriched["output_tokens"] = output_tokens
    enriched["total_tokens"] = total_tokens
    enriched["fresh_total_tokens"] = fresh_total_tokens
    enriched["reasoning_tokens"] = reasoning_tokens
    enriched["cache_ratio"] = cache_ratio
    return enriched


def _build_summary_payload(daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    local_today = datetime.now(settings.timezone).date()
    local_yesterday = local_today - timedelta(days=1)
    seven_days_ago = local_today - timedelta(days=6)

    rows_by_day = {row["local_day"]: row for row in daily_rows}

    today_row = _build_day_bucket(local_today.isoformat(), rows_by_day.get(local_today.isoformat()))
    yesterday_row = _build_day_bucket(
        local_yesterday.isoformat(),
        rows_by_day.get(local_yesterday.isoformat()),
    )

    trailing_7_row = {
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
    }
    for row in daily_rows:
        row_day = datetime.fromisoformat(row["local_day"]).date()
        if row_day >= seven_days_ago:
            trailing_7_row["requests"] += row["requests"] or 0
            trailing_7_row["input_tokens"] += row["input_tokens"] or 0
            trailing_7_row["cached_input_tokens"] += row["cached_input_tokens"] or 0
            trailing_7_row["output_tokens"] += row["output_tokens"] or 0
            trailing_7_row["total_tokens"] += row["total_tokens"] or 0
            trailing_7_row["reasoning_tokens"] += row["reasoning_tokens"] or 0

    return {
        "timezone": settings.timezone_name,
        "today": today_row,
        "yesterday": yesterday_row,
        "last_7_days": _enrich_usage_row(trailing_7_row),
    }


def _build_day_bucket(date_text: str, row: dict[str, Any] | None) -> dict[str, Any]:
    bucket = {
        "date": date_text,
        "requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
    }
    if row:
        bucket.update(_enrich_usage_row(row))
    return bucket


def _delta_text(current: int, previous: int, noun: str) -> str:
    delta = current - previous
    if delta == 0:
        return f"No change vs yesterday ({noun})"
    sign = "+" if delta > 0 else "-"
    return f"{sign}{abs(delta):,} {noun} vs yesterday"


def _render_mix_bar(row: dict[str, Any]) -> str:
    total_tokens = max(_safe_int(row.get("total_tokens")), 0)
    cached = max(_safe_int(row.get("cached_input_tokens")), 0)
    fresh = max(_safe_int(row.get("fresh_input_tokens")), 0)
    output = max(_safe_int(row.get("output_tokens")), 0)

    if total_tokens <= 0:
        return '<div class="mix-bar empty"><span class="empty-label">No usage</span></div>'

    segments = []
    for label, class_name, value in (
        ("Cached input", "cached", cached),
        ("Fresh input", "fresh", fresh),
        ("Output", "output", output),
    ):
        if value <= 0:
            continue
        width = max((value / total_tokens) * 100, 1.2)
        segments.append(
            f'<span class="seg {class_name}" style="width:{width:.2f}%" title="{label}: {value:,}"></span>'
        )

    return f'<div class="mix-bar">{"".join(segments)}</div>'


def _render_dashboard(
    *,
    summary: dict[str, Any],
    daily_rows: list[dict[str, Any]],
    model_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
    upstream_rows: list[dict[str, Any]],
    selected_upstream: str | None,
    origin: str,
) -> str:
    filter_names = [settings.default_upstream_name]
    for upstream in settings.upstreams.values():
        if upstream.name not in filter_names:
            filter_names.append(upstream.name)
    for row in upstream_rows:
        upstream_name = str(row["upstream_name"])
        if upstream_name not in filter_names:
            filter_names.append(upstream_name)
    if selected_upstream is not None and selected_upstream not in filter_names:
        filter_names.append(selected_upstream)

    filter_links = [
        (
            "All APIs",
            "/dashboard",
            selected_upstream is None,
        )
    ]
    for upstream_name in filter_names:
        upstream_meta = _upstream_meta(upstream_name)
        filter_links.append(
            (
                upstream_meta["label"],
                f"/dashboard?upstream={html.escape(upstream_name)}",
                selected_upstream == upstream_name,
            )
        )

    filter_link_html = []
    for label, href, is_active in filter_links:
        class_name = "pill active" if is_active else "pill"
        filter_link_html.append(
            f'<a class="{class_name}" href="{href}">{html.escape(label)}</a>'
        )

    selector_options = [
        (
            "/dashboard",
            "All APIs",
            selected_upstream is None,
        )
    ]
    for upstream_name in filter_names:
        upstream_meta = _upstream_meta(upstream_name)
        selector_options.append(
            (
                f"/dashboard?upstream={html.escape(upstream_name)}",
                upstream_meta["label"],
                selected_upstream == upstream_name,
            )
        )

    selector_options_html = []
    for href, label, is_selected in selector_options:
        selected_attr = " selected" if is_selected else ""
        selector_options_html.append(
            f'<option value="{html.escape(href, quote=True)}"{selected_attr}>{html.escape(label)}</option>'
        )

    route_rows = []
    for upstream in settings.upstreams.values():
        route_rows.append(
            f"""
            <tr>
                <td>{html.escape(upstream.label)}</td>
                <td>{html.escape(_local_base_url(upstream.name, origin=origin))}</td>
                <td>{'yes' if upstream.name == settings.default_upstream_name else 'no'}</td>
            </tr>
            """
        )

    upstream_table_rows = []
    for row in upstream_rows:
        upstream_table_rows.append(
            f"""
            <tr>
                <td>{html.escape(str(row["label"]))}</td>
                <td>{html.escape(str(row["local_base_url"]))}</td>
                <td>{row["requests"]}</td>
                <td>{row["input_tokens"] or 0:,}</td>
                <td>{row["cached_input_tokens"] or 0:,}</td>
                <td>{row["fresh_input_tokens"] or 0:,}</td>
                <td>{row["output_tokens"] or 0:,}</td>
                <td>{row["total_tokens"] or 0:,}</td>
                <td>{_render_mix_bar(row)}</td>
            </tr>
            """
        )

    daily_table_rows = []
    for row in daily_rows:
        daily_table_rows.append(
            f"""
            <tr>
                <td>{html.escape(row["local_day"])}</td>
                <td>{row["requests"]}</td>
                <td>{row["input_tokens"] or 0:,}</td>
                <td>{row["cached_input_tokens"] or 0:,}</td>
                <td>{row["fresh_input_tokens"] or 0:,}</td>
                <td>{row["output_tokens"] or 0:,}</td>
                <td>{row["total_tokens"] or 0:,}</td>
                <td>{_render_mix_bar(row)}</td>
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
                <td>{row["cached_input_tokens"] or 0:,}</td>
                <td>{row["fresh_input_tokens"] or 0:,}</td>
                <td>{row["output_tokens"] or 0:,}</td>
                <td>{row["total_tokens"] or 0:,}</td>
                <td>{_render_mix_bar(row)}</td>
            </tr>
            """
        )

    recent_table_rows = []
    for row in recent_rows:
        upstream_meta = _upstream_meta(row["upstream_name"])
        recent_table_rows.append(
            f"""
            <tr>
                <td>{html.escape(row["ts_utc"])}</td>
                <td>{html.escape(str(upstream_meta["label"]))}</td>
                <td>{html.escape(row["endpoint"])}</td>
                <td>{html.escape(str(row["model"] or "(unknown)"))}</td>
                <td>{row["input_tokens"] or 0:,}</td>
                <td>{row["cached_input_tokens"] or 0:,}</td>
                <td>{row["fresh_input_tokens"] or 0:,}</td>
                <td>{row["output_tokens"] or 0:,}</td>
                <td>{row["total_tokens"] or 0:,}</td>
                <td>{row["latency_ms"]} ms</td>
                <td>{'yes' if row["is_stream"] else 'no'}</td>
            </tr>
            """
        )

    selected_label = _selected_upstream_label(selected_upstream)
    non_default_upstream = next(
        (
            upstream.name
            for upstream in settings.upstreams.values()
            if upstream.name != settings.default_upstream_name
        ),
        settings.default_upstream_name,
    )
    selected_meta = _upstream_meta(selected_upstream, origin=origin) if selected_upstream else None
    selected_route = (
        str(selected_meta["local_base_url"])
        if selected_meta is not None and selected_meta["is_configured"]
        else None
    )
    if selected_meta is None:
        selected_route_text = "Combined view across all configured APIs"
        selected_route_label = "Current scope"
    elif selected_route is not None:
        selected_route_text = selected_route
        selected_route_label = "Selected local route"
    else:
        selected_route_text = "Showing historical usage for a source that is not currently configured as a live route."
        selected_route_label = "Historical scope"
    today = summary["today"]
    yesterday = summary["yesterday"]
    last_7_days = summary["last_7_days"]
    today_mix_bar = _render_mix_bar(today)

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
      --accent: #1157d7;
      --accent-soft: #e8f0ff;
      --fresh: #f29f05;
      --fresh-soft: #fff1d3;
      --cached: #1b9c85;
      --cached-soft: #def7f1;
      --output: #6747ff;
      --output-soft: #ece7ff;
      --shadow: 0 14px 36px rgba(16, 34, 58, 0.08);
    }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(17, 87, 215, 0.12), transparent 24rem),
        linear-gradient(180deg, #edf4ff 0%, var(--bg) 100%);
      color: var(--ink);
      font-family: "Segoe UI", "Trebuchet MS", Arial, sans-serif;
    }}
    .wrap {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    h1 {{
      font-size: clamp(2rem, 4vw, 3.2rem);
      letter-spacing: -0.04em;
    }}
    p {{
      color: var(--muted);
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(300px, 0.9fr);
      gap: 18px;
      margin-bottom: 18px;
    }}
    .hero-card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 22px;
      box-shadow: var(--shadow);
    }}
    .hero-card.primary {{
      background:
        linear-gradient(135deg, rgba(17, 87, 215, 0.08), rgba(255, 255, 255, 0.94)),
        var(--card);
    }}
    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.76rem;
      color: var(--accent);
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .subcopy {{
      max-width: 62ch;
      margin-top: 14px;
    }}
    .hero-meta {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .meta-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px 16px;
      background: rgba(255, 255, 255, 0.72);
    }}
    .meta-label {{
      font-size: 0.8rem;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .meta-value {{
      font-size: 1rem;
      font-weight: 700;
      word-break: break-all;
    }}
    .selector {{
      display: grid;
      gap: 10px;
    }}
    .selector label {{
      font-size: 0.84rem;
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .selector select {{
      width: 100%;
      border: 1px solid #c7d4e6;
      border-radius: 14px;
      padding: 12px 14px;
      font: inherit;
      background: white;
      color: var(--ink);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 16px;
      margin: 20px 0 28px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px 18px 16px;
      box-shadow: var(--shadow);
    }}
    .card.raw {{
      border-top: 5px solid var(--accent);
    }}
    .card.fresh {{
      border-top: 5px solid var(--fresh);
      background: linear-gradient(180deg, var(--fresh-soft), var(--card) 52%);
    }}
    .card.cached {{
      border-top: 5px solid var(--cached);
      background: linear-gradient(180deg, var(--cached-soft), var(--card) 52%);
    }}
    .card.output {{
      border-top: 5px solid var(--output);
      background: linear-gradient(180deg, var(--output-soft), var(--card) 52%);
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
    .micro {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.84rem;
    }}
    .pills {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 18px 0 10px;
    }}
    .pill {{
      text-decoration: none;
      color: #0f4db8;
      background: #eef4ff;
      border: 1px solid #cfe0ff;
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 0.92rem;
    }}
    .pill.active {{
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }}
    .legend {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 14px;
    }}
    .legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.88rem;
      color: var(--muted);
    }}
    .legend-swatch {{
      width: 11px;
      height: 11px;
      border-radius: 999px;
      display: inline-block;
    }}
    .legend-swatch.cached {{
      background: var(--cached);
    }}
    .legend-swatch.fresh {{
      background: var(--fresh);
    }}
    .legend-swatch.output {{
      background: var(--output);
    }}
    .table-shell {{
      overflow-x: auto;
      border-radius: 18px;
      box-shadow: var(--shadow);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
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
    .mix-bar {{
      width: 100%;
      min-width: 170px;
      height: 12px;
      background: #edf2f8;
      border-radius: 999px;
      overflow: hidden;
      display: flex;
    }}
    .mix-bar.empty {{
      display: grid;
      place-items: center;
      min-width: 130px;
      height: 24px;
      color: var(--muted);
      font-size: 0.8rem;
    }}
    .empty-label {{
      padding: 0 8px;
    }}
    .seg {{
      display: block;
      height: 100%;
    }}
    .seg.cached {{
      background: linear-gradient(90deg, #178a74, var(--cached));
    }}
    .seg.fresh {{
      background: linear-gradient(90deg, #d88802, var(--fresh));
    }}
    .seg.output {{
      background: linear-gradient(90deg, #4b2ee0, var(--output));
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
    .hint {{
      margin-top: 8px;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: var(--accent-soft);
      color: #163661;
    }}
    .headline-stat {{
      margin-top: 16px;
      display: grid;
      gap: 10px;
    }}
    @media (max-width: 700px) {{
      .wrap {{
        padding: 20px 14px 36px;
      }}
      .hero {{
        grid-template-columns: 1fr;
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
    <div class="hero">
      <section class="hero-card primary">
        <div class="eyebrow">Token Usage Console</div>
        <h1>Daily usage, split into raw, cached, and fresh input.</h1>
        <p class="subcopy">
          Current view: <strong>{html.escape(selected_label)}</strong>.
          This page separates repeated cached context from newly added input so you can read daily usage more accurately.
        </p>
        <div class="headline-stat">
          {today_mix_bar}
          <div class="legend">
            <span class="legend-item"><span class="legend-swatch cached"></span>Cached input</span>
            <span class="legend-item"><span class="legend-swatch fresh"></span>Fresh input</span>
            <span class="legend-item"><span class="legend-swatch output"></span>Output</span>
          </div>
        </div>
        <div class="hero-meta">
          <div class="meta-card">
            <div class="meta-label">Timezone</div>
            <div class="meta-value">{html.escape(summary["timezone"])}</div>
          </div>
          <div class="meta-card">
            <div class="meta-label">{html.escape(selected_route_label)}</div>
            <div class="meta-value">{html.escape(selected_route_text)}</div>
          </div>
        </div>
      </section>

      <aside class="hero-card">
        <div class="eyebrow">Source Filter</div>
        <div class="selector">
          <label for="upstream-select">Choose API source</label>
          <select id="upstream-select" onchange="if (this.value) window.location=this.value;">
            {''.join(selector_options_html)}
          </select>
        </div>
        <div class="pills">
          {''.join(filter_link_html)}
        </div>
        <div class="hint">
          Default upstream route:
          <span class="code">{html.escape(_local_base_url(settings.default_upstream_name, origin=origin))}</span><br>
          Example named route:
          <span class="code">{html.escape(_local_base_url(non_default_upstream, origin=origin))}</span>
        </div>
      </aside>
    </div>

    <div class="grid">
      <div class="card raw">
        <div class="label">Today Total</div>
        <div class="metric">{today["total_tokens"]:,}</div>
        <div class="micro">{_delta_text(today["total_tokens"], yesterday["total_tokens"], "tokens")}</div>
      </div>
      <div class="card raw">
        <div class="label">Today Raw Input</div>
        <div class="metric">{today["input_tokens"]:,}</div>
        <div class="micro">All input tokens sent today, including cached context</div>
      </div>
      <div class="card fresh">
        <div class="label">Today Fresh Input</div>
        <div class="metric">{today["fresh_input_tokens"]:,}</div>
        <div class="micro">New input after subtracting cached input</div>
      </div>
      <div class="card cached">
        <div class="label">Today Cached Input</div>
        <div class="metric">{today["cached_input_tokens"]:,}</div>
        <div class="micro">{today["cache_ratio"]:.0%} of today&apos;s input was cached</div>
      </div>
      <div class="card output">
        <div class="label">Today Output</div>
        <div class="metric">{today["output_tokens"]:,}</div>
        <div class="micro">Model-generated output tokens today</div>
      </div>
      <div class="card">
        <div class="label">Today Requests</div>
        <div class="metric">{today["requests"]:,}</div>
        <div class="micro">{_delta_text(today["requests"], yesterday["requests"], "requests")}</div>
      </div>
      <div class="card">
        <div class="label">Last 7 Days Total</div>
        <div class="metric">{last_7_days["total_tokens"]:,}</div>
        <div class="micro">{last_7_days["requests"]:,} requests in the trailing 7-day window</div>
      </div>
    </div>

    <div class="section">
      <h2>Configured Routes</h2>
      <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>API Source</th>
            <th>Local Base URL</th>
            <th>Default Route</th>
          </tr>
        </thead>
        <tbody>
          {''.join(route_rows)}
        </tbody>
      </table>
      </div>
    </div>

    <div class="section">
      <h2>API Sources (Last 7 Days)</h2>
      <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>API Source</th>
            <th>Local Base URL</th>
            <th>Requests</th>
            <th>Raw Input</th>
            <th>Cached</th>
            <th>Fresh Input</th>
            <th>Output</th>
            <th>Total</th>
            <th>Mix</th>
          </tr>
        </thead>
        <tbody>
          {''.join(upstream_table_rows) if upstream_table_rows else '<tr><td colspan="9">No usage recorded yet.</td></tr>'}
        </tbody>
      </table>
      </div>
    </div>

    <div class="section">
      <h2>Daily Totals</h2>
      <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>Day</th>
            <th>Requests</th>
            <th>Raw Input</th>
            <th>Cached</th>
            <th>Fresh Input</th>
            <th>Output</th>
            <th>Total</th>
            <th>Mix</th>
          </tr>
        </thead>
        <tbody>
          {''.join(daily_table_rows) if daily_table_rows else '<tr><td colspan="8">No usage recorded yet.</td></tr>'}
        </tbody>
      </table>
      </div>
    </div>

    <div class="section">
      <h2>Models (Last 7 Days)</h2>
      <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Requests</th>
            <th>Raw Input</th>
            <th>Cached</th>
            <th>Fresh Input</th>
            <th>Output</th>
            <th>Total</th>
            <th>Mix</th>
          </tr>
        </thead>
        <tbody>
          {''.join(model_table_rows) if model_table_rows else '<tr><td colspan="8">No usage recorded yet.</td></tr>'}
        </tbody>
      </table>
      </div>
    </div>

    <div class="section">
      <h2>Recent Requests</h2>
      <div class="table-shell">
      <table>
        <thead>
          <tr>
            <th>Timestamp (UTC)</th>
            <th>API Source</th>
            <th>Endpoint</th>
            <th>Model</th>
            <th>Raw Input</th>
            <th>Cached</th>
            <th>Fresh Input</th>
            <th>Output</th>
            <th>Total Tokens</th>
            <th>Latency</th>
            <th>Stream</th>
          </tr>
        </thead>
        <tbody>
          {''.join(recent_table_rows) if recent_table_rows else '<tr><td colspan="11">No requests logged yet.</td></tr>'}
        </tbody>
      </table>
      </div>
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
