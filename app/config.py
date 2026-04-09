from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "usage.db"


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _read_json(name: str) -> Any:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Environment variable {name} must contain valid JSON.") from exc


def _read_json_file(path: Path) -> Any:
    try:
        raw_value = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"Failed to read upstream config file: {path}") from exc

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Upstream config file must contain valid JSON: {path}") from exc


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return normalized


def _load_timezone(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        if timezone_name.upper() == "UTC":
            return timezone.utc
        raise RuntimeError(
            f"Timezone '{timezone_name}' is not available. "
            "Install the 'tzdata' package or choose a different timezone."
        ) from exc


@dataclass(frozen=True)
class UpstreamConfig:
    name: str
    base_url: str
    api_key: str | None = None
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True)
class Settings:
    db_path: Path
    timezone_name: str
    listen_host: str
    listen_port: int
    dashboard_days: int
    request_timeout_s: float
    force_chat_stream_usage: bool
    write_batch_size: int
    default_upstream_name: str
    upstreams: dict[str, UpstreamConfig]
    upstreams_file: Path | None

    @property
    def timezone(self) -> tzinfo:
        return _load_timezone(self.timezone_name)

    @property
    def default_upstream(self) -> UpstreamConfig:
        return self.upstreams[self.default_upstream_name]


def load_settings() -> Settings:
    timezone_name = os.getenv("TOKEN_PROXY_TIMEZONE", "UTC")
    _load_timezone(timezone_name)

    db_path = Path(os.getenv("TOKEN_PROXY_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
    default_upstream_name = os.getenv("TOKEN_PROXY_DEFAULT_UPSTREAM", "default").strip() or "default"
    default_display_name = os.getenv("TOKEN_PROXY_DEFAULT_UPSTREAM_LABEL", "Default API").strip() or "Default API"
    _validate_upstream_name(default_upstream_name)

    upstreams = {
        default_upstream_name: UpstreamConfig(
            name=default_upstream_name,
            base_url=_normalize_base_url(
                os.getenv("OPENAI_UPSTREAM_BASE_URL", "https://api.openai.com")
            ),
            api_key=os.getenv("OPENAI_UPSTREAM_API_KEY"),
            display_name=default_display_name,
        )
    }

    upstreams_file = _resolve_upstreams_file()
    if upstreams_file is not None and upstreams_file.exists():
        _merge_upstream_entries(
            upstreams,
            _read_json_file(upstreams_file),
            source=f"file {upstreams_file}",
        )

    upstream_entries = _read_json("TOKEN_PROXY_UPSTREAMS_JSON")
    if upstream_entries is not None:
        _merge_upstream_entries(
            upstreams,
            upstream_entries,
            source="TOKEN_PROXY_UPSTREAMS_JSON",
        )

    return Settings(
        db_path=db_path,
        timezone_name=timezone_name,
        listen_host=os.getenv("TOKEN_PROXY_HOST", "127.0.0.1"),
        listen_port=int(os.getenv("TOKEN_PROXY_PORT", "8787")),
        dashboard_days=max(1, int(os.getenv("TOKEN_PROXY_DASHBOARD_DAYS", "14"))),
        request_timeout_s=float(os.getenv("TOKEN_PROXY_REQUEST_TIMEOUT_S", "600")),
        force_chat_stream_usage=_read_bool("TOKEN_PROXY_FORCE_CHAT_STREAM_USAGE", True),
        write_batch_size=max(1, int(os.getenv("TOKEN_PROXY_WRITE_BATCH_SIZE", "50"))),
        default_upstream_name=default_upstream_name,
        upstreams=upstreams,
        upstreams_file=upstreams_file if upstreams_file is not None and upstreams_file.exists() else None,
    )


def _parse_upstream_config(upstream_name: str, raw_entry: Any) -> UpstreamConfig:
    _validate_upstream_name(upstream_name)

    if isinstance(raw_entry, str):
        return UpstreamConfig(
            name=upstream_name,
            base_url=_normalize_base_url(raw_entry),
        )

    if not isinstance(raw_entry, dict):
        raise RuntimeError(
            "Each upstream entry in TOKEN_PROXY_UPSTREAMS_JSON must be either a base URL string "
            "or an object with at least a 'base_url' field."
        )

    raw_base_url = raw_entry.get("base_url")
    if not isinstance(raw_base_url, str) or not raw_base_url.strip():
        raise RuntimeError(f"Upstream '{upstream_name}' must include a non-empty 'base_url'.")

    api_key = raw_entry.get("api_key")
    api_key_env = raw_entry.get("api_key_env")
    if api_key is not None and not isinstance(api_key, str):
        raise RuntimeError(f"Upstream '{upstream_name}' field 'api_key' must be a string when provided.")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise RuntimeError(f"Upstream '{upstream_name}' field 'api_key_env' must be a string when provided.")

    resolved_api_key = api_key
    if resolved_api_key is None and api_key_env:
        resolved_api_key = os.getenv(api_key_env)

    display_name = raw_entry.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise RuntimeError(f"Upstream '{upstream_name}' field 'display_name' must be a string when provided.")

    return UpstreamConfig(
        name=upstream_name,
        base_url=_normalize_base_url(raw_base_url),
        api_key=resolved_api_key,
        display_name=display_name,
    )


def _resolve_upstreams_file() -> Path | None:
    configured = os.getenv("TOKEN_PROXY_UPSTREAMS_FILE")
    if configured:
        return Path(configured).expanduser()

    default_file = PROJECT_ROOT / "config" / "upstreams.json"
    if default_file.exists():
        return default_file
    return None


def _merge_upstream_entries(
    upstreams: dict[str, UpstreamConfig],
    raw_entries: Any,
    *,
    source: str,
) -> None:
    if not isinstance(raw_entries, dict):
        raise RuntimeError(f"{source} must be a JSON object keyed by upstream name.")

    for upstream_name, raw_entry in raw_entries.items():
        if not isinstance(upstream_name, str) or not upstream_name.strip():
            raise RuntimeError(f"Each upstream in {source} must have a non-empty string name.")
        upstreams[upstream_name] = _parse_upstream_config(upstream_name, raw_entry)


def _validate_upstream_name(name: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise RuntimeError(
            f"Invalid upstream name '{name}'. Use only letters, digits, underscore, or dash."
        )


settings = load_settings()
