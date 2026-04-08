from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "usage.db"


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    upstream_api_key: str | None
    db_path: Path
    timezone_name: str
    listen_host: str
    listen_port: int
    dashboard_days: int
    request_timeout_s: float
    force_chat_stream_usage: bool

    @property
    def timezone(self) -> tzinfo:
        return _load_timezone(self.timezone_name)


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


def load_settings() -> Settings:
    timezone_name = os.getenv("TOKEN_PROXY_TIMEZONE", "UTC")
    _load_timezone(timezone_name)

    upstream_base_url = os.getenv(
        "OPENAI_UPSTREAM_BASE_URL",
        "https://api.openai.com",
    ).rstrip("/")
    if upstream_base_url.endswith("/v1"):
        upstream_base_url = upstream_base_url[:-3]

    db_path = Path(os.getenv("TOKEN_PROXY_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()

    return Settings(
        upstream_base_url=upstream_base_url,
        upstream_api_key=os.getenv("OPENAI_UPSTREAM_API_KEY"),
        db_path=db_path,
        timezone_name=timezone_name,
        listen_host=os.getenv("TOKEN_PROXY_HOST", "127.0.0.1"),
        listen_port=int(os.getenv("TOKEN_PROXY_PORT", "8787")),
        dashboard_days=max(1, int(os.getenv("TOKEN_PROXY_DASHBOARD_DAYS", "14"))),
        request_timeout_s=float(os.getenv("TOKEN_PROXY_REQUEST_TIMEOUT_S", "600")),
        force_chat_stream_usage=_read_bool(
            "TOKEN_PROXY_FORCE_CHAT_STREAM_USAGE",
            True,
        ),
    )


settings = load_settings()
