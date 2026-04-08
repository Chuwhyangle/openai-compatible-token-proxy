from __future__ import annotations

import codecs
import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedUsage:
    request_id: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    raw_usage: dict[str, Any]


def fingerprint_api_key(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None

    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    api_key = parts[1].strip()
    if not api_key:
        return None

    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]
    suffix = api_key[-4:] if len(api_key) >= 4 else api_key
    return f"{suffix}:{key_hash}"


def decode_json_body(content_type: str | None, body: bytes) -> dict[str, Any] | None:
    if not body or not content_type:
        return None
    if "application/json" not in content_type.lower():
        return None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def maybe_enable_chat_stream_usage(
    *,
    path: str,
    content_type: str | None,
    body: bytes,
    force_enabled: bool,
) -> tuple[bytes, dict[str, Any] | None]:
    payload = decode_json_body(content_type, body)

    if not force_enabled or path != "chat/completions" or payload is None:
        return body, payload

    if not payload.get("stream"):
        return body, payload

    stream_options = payload.get("stream_options")
    if stream_options is None:
        stream_options = {}
        payload["stream_options"] = stream_options

    if not isinstance(stream_options, dict):
        return body, payload

    if "include_usage" in stream_options:
        return body, payload

    stream_options["include_usage"] = True
    patched_body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return patched_body, payload


def normalize_usage(payload: dict[str, Any] | None) -> NormalizedUsage | None:
    if not isinstance(payload, dict):
        return None

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    model = _coerce_string(payload.get("model"))
    request_id = _coerce_string(payload.get("id"))

    if "input_tokens" in usage or "output_tokens" in usage:
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        input_tokens = _coerce_int(usage.get("input_tokens"))
        output_tokens = _coerce_int(usage.get("output_tokens"))
        total_tokens = _coerce_int(usage.get("total_tokens"), input_tokens + output_tokens)
        cached_input_tokens = _coerce_int(input_details.get("cached_tokens"))
        reasoning_tokens = _coerce_int(output_details.get("reasoning_tokens"))
        return NormalizedUsage(
            request_id=request_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            raw_usage=usage,
        )

    if (
        "prompt_tokens" in usage
        or "completion_tokens" in usage
        or "total_tokens" in usage
    ):
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        input_tokens = _coerce_int(usage.get("prompt_tokens"))
        output_tokens = _coerce_int(usage.get("completion_tokens"))
        total_tokens = _coerce_int(usage.get("total_tokens"), input_tokens + output_tokens)
        cached_input_tokens = _coerce_int(prompt_details.get("cached_tokens"))
        reasoning_tokens = _coerce_int(completion_details.get("reasoning_tokens"))
        return NormalizedUsage(
            request_id=request_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            raw_usage=usage,
        )

    return None


def extract_usage_from_stream_event(event_payload: dict[str, Any]) -> NormalizedUsage | None:
    direct_usage = normalize_usage(event_payload)
    if direct_usage is not None:
        return direct_usage

    response_payload = event_payload.get("response")
    if isinstance(response_payload, dict):
        nested_usage = normalize_usage(response_payload)
        if nested_usage is not None:
            return nested_usage

    item_payload = event_payload.get("item")
    if isinstance(item_payload, dict):
        nested_usage = normalize_usage(item_payload)
        if nested_usage is not None:
            return nested_usage

    return None


class StreamingUsageTap:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self.usage: NormalizedUsage | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += self._decoder.decode(chunk)
        self._consume(final=False)

    def finalize(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        self._consume(final=True)

    def _consume(self, *, final: bool) -> None:
        self._buffer = self._buffer.replace("\r\n", "\n").replace("\r", "\n")

        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            self._handle_block(block)

        if final and self._buffer.strip():
            self._handle_block(self._buffer)
            self._buffer = ""

    def _handle_block(self, block: str) -> None:
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if not data_lines:
            return

        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            return

        try:
            event_payload = json.loads(data)
        except json.JSONDecodeError:
            return

        usage = extract_usage_from_stream_event(event_payload)
        if usage is not None:
            self.usage = usage


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
