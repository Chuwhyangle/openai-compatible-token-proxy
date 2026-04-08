# openai-compatible-token-proxy

A lightweight local proxy for OpenAI-compatible APIs that records token usage in
SQLite and shows daily stats in a simple dashboard.

## Why this exists

If you only have a normal API key and do not have organization admin access,
you cannot reliably query daily usage from the provider side. This proxy solves
that by recording usage locally for every request that passes through it.

## Features

- Local OpenAI-compatible proxy on `127.0.0.1`
- SQLite-backed daily usage tracking
- Streaming-aware logging for `responses` and `chat/completions`
- Simple local dashboard for daily and per-model totals
- Does not store prompt or response text
- Works with official OpenAI endpoints and OpenAI-compatible relay services

## Endpoints

- Proxy: `http://127.0.0.1:8787/v1/...`
- Dashboard: `http://127.0.0.1:8787/dashboard`
- Health: `http://127.0.0.1:8787/healthz`

## Quick start

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Set your upstream endpoint and API key.
5. Run the server.
6. Point your tools to the local `base_url`.

```powershell
git clone https://github.com/your-name/openai-compatible-token-proxy.git
cd openai-compatible-token-proxy
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
$env:OPENAI_UPSTREAM_BASE_URL = "https://api.openai.com/v1"
$env:OPENAI_UPSTREAM_API_KEY = "sk-your-upstream-key"
py -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

## Environment variables

- `OPENAI_UPSTREAM_BASE_URL`
  - Default: `https://api.openai.com`
  - You can set either `https://api.openai.com` or `https://api.openai.com/v1`
  - The proxy normalizes a trailing `/v1` automatically
  - OpenAI-compatible upstreams such as `https://api.example.com/v1` are also supported
- `OPENAI_UPSTREAM_API_KEY`
  - Optional fallback API key if incoming requests do not include
    `Authorization: Bearer ...`
- `TOKEN_PROXY_DB_PATH`
  - Default: `./data/usage.db`
- `TOKEN_PROXY_TIMEZONE`
  - Default: `UTC`
- `TOKEN_PROXY_FORCE_CHAT_STREAM_USAGE`
  - Default: `true`
  - When enabled, the proxy adds `stream_options.include_usage=true` for
    streaming `chat/completions` requests if the client did not set it.
- `TOKEN_PROXY_REQUEST_TIMEOUT_S`
  - Default: `600`

## Example Python client

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="http://127.0.0.1:8787/v1",
)

response = client.responses.create(
    model="gpt-5-mini",
    input="Say hello in one short sentence.",
)

print(response.output_text)
```

## Optional .env file

Create a local `.env` file if you do not want to export environment variables
manually. A safe template is included in `.env.example`.

```powershell
Copy-Item .env.example .env
py -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8787
```

## Notes

- This tracks only traffic that goes through the local proxy.
- If another machine or tool uses the same key without this proxy, that usage
  will not appear in the local database.
- The proxy is optimized for JSON-based OpenAI endpoints. Large file uploads are
  forwarded, but the implementation is focused on text endpoints.
