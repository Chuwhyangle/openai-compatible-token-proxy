# openai-compatible-token-proxy

A lightweight local proxy for OpenAI-compatible APIs that records token usage in
SQLite and shows daily stats in a simple dashboard.

## What's new in this version

- Multi-upstream routing with a default `/v1/...` route plus named routes like `/proxy/<name>/v1/...`
- A redesigned dashboard with API-source filtering and clearer visual grouping
- Token breakdowns for raw input, cached input, fresh input, output, and total usage
- Better concurrency handling through batched SQLite writes
- Streaming-aware usage capture for `responses` and `chat/completions`
- A Windows-friendly `start.bat` launcher for double-click startup
- More resilient historical rendering, so old upstream names in the database do not break the dashboard

## Why this exists

If you only have normal API keys and do not have organization admin access, you
cannot reliably query daily usage from the provider side. This proxy solves
that by recording usage locally for every request that passes through it.

## Design goal for scaling

This project is now configuration-driven for multiple upstream APIs. That means
adding a new API should usually be a configuration change, not a code change.

The intended scaling path is:

1. Keep one default upstream on `/v1`
2. Add more named upstreams in config
3. Point different tools to different local proxy routes
4. View combined or per-source stats in the dashboard

## Features

- Local OpenAI-compatible proxy on `127.0.0.1`
- Supports one default upstream plus any number of named upstreams
- Named upstreams can be loaded from environment JSON or a local config file
- SQLite-backed daily usage tracking
- Batched background writes for better concurrency under parallel traffic
- Streaming-aware logging for `responses` and `chat/completions`
- Simple local dashboard with per-API-source filtering
- Does not store prompt or response text
- Works with official OpenAI endpoints and OpenAI-compatible relay services

## Endpoints

- Default proxy: `http://127.0.0.1:8787/v1/...`
- Named upstream proxy: `http://127.0.0.1:8787/proxy/<name>/v1/...`
- Dashboard: `http://127.0.0.1:8787/dashboard`
- Upstreams list: `http://127.0.0.1:8787/upstreams`
- Health: `http://127.0.0.1:8787/healthz`

## Quick start

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Set your default upstream endpoint and API key.
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

## The recommended way to scale to more APIs

Use a local config file:

1. Copy the template:

```powershell
New-Item -ItemType Directory -Path .\config -Force | Out-Null
Copy-Item .\config\upstreams.example.json .\config\upstreams.json
```

2. Edit `config/upstreams.json` and add one block per upstream:

```json
{
  "relay_a": {
    "base_url": "https://relay-a.example.com/v1",
    "api_key_env": "RELAY_A_UPSTREAM_API_KEY",
    "display_name": "Relay A"
  },
  "relay_b": {
    "base_url": "https://relay-b.example.com/v1",
    "api_key_env": "RELAY_B_UPSTREAM_API_KEY",
    "display_name": "Relay B"
  }
}
```

3. Export the referenced secrets:

```powershell
$env:RELAY_A_UPSTREAM_API_KEY = "sk-your-relay-a-key"
$env:RELAY_B_UPSTREAM_API_KEY = "sk-your-relay-b-key"
```

4. Start the proxy:

```powershell
py -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

With that setup:

- Default upstream stays on `http://127.0.0.1:8787/v1`
- `relay_a` becomes `http://127.0.0.1:8787/proxy/relay_a/v1`
- `relay_b` becomes `http://127.0.0.1:8787/proxy/relay_b/v1`

## Multiple upstreams via environment JSON

If you prefer not to use a file, you can still configure named upstreams through
`TOKEN_PROXY_UPSTREAMS_JSON`.

Example:

```powershell
$env:OPENAI_UPSTREAM_BASE_URL = "https://api.openai.com/v1"
$env:TOKEN_PROXY_DEFAULT_UPSTREAM_LABEL = "Primary Relay"
$env:TOKEN_PROXY_UPSTREAMS_JSON = '{"relay_a":{"base_url":"https://relay-a.example.com/v1","api_key_env":"RELAY_A_UPSTREAM_API_KEY","display_name":"Relay A"}}'
$env:RELAY_A_UPSTREAM_API_KEY = "sk-your-relay-a-key"
py -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

## Environment variables

- `OPENAI_UPSTREAM_BASE_URL`
  - Default: `https://api.openai.com`
  - You can set either `https://api.openai.com` or `https://api.openai.com/v1`
  - The proxy normalizes a trailing `/v1` automatically
  - OpenAI-compatible upstreams such as `https://api.example.com/v1` are also supported
- `OPENAI_UPSTREAM_API_KEY`
  - Optional fallback API key for the default upstream if incoming requests do not include `Authorization: Bearer ...`
- `TOKEN_PROXY_DEFAULT_UPSTREAM`
  - Default: `default`
  - Controls which configured upstream is used for `/v1/...`
- `TOKEN_PROXY_DEFAULT_UPSTREAM_LABEL`
  - Default: `Default API`
  - Human-friendly label shown in the dashboard
- `TOKEN_PROXY_UPSTREAMS_FILE`
  - Optional path to a JSON file containing named upstream definitions
  - Default discovery path: `./config/upstreams.json` if the file exists
- `TOKEN_PROXY_UPSTREAMS_JSON`
  - Optional JSON object keyed by upstream name
  - Overrides or supplements entries loaded from the file
- `TOKEN_PROXY_DB_PATH`
  - Default: `./data/usage.db`
- `TOKEN_PROXY_TIMEZONE`
  - Default: `UTC`
- `TOKEN_PROXY_FORCE_CHAT_STREAM_USAGE`
  - Default: `true`
  - When enabled, the proxy adds `stream_options.include_usage=true` for streaming `chat/completions` requests if the client did not set it.
- `TOKEN_PROXY_REQUEST_TIMEOUT_S`
  - Default: `600`
- `TOKEN_PROXY_WRITE_BATCH_SIZE`
  - Default: `50`
  - Number of usage rows batched together before committing to SQLite

## Naming rules for upstreams

Upstream names become part of the local route, so they should use only:

- letters
- digits
- underscore
- dash

Examples:

- `relay_a`
- `relay_2`
- `azure-west`

## Example Python clients

Default upstream:

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="http://127.0.0.1:8787/v1",
)
```

Named upstream:

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="http://127.0.0.1:8787/proxy/relay_a/v1",
)
```

## Optional .env file

Create a local `.env` file if you do not want to export environment variables
manually. A safe template is included in `.env.example`.

```powershell
Copy-Item .env.example .env
py -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8787
```

## Windows double-click startup

If you prefer not to open a terminal manually, use the included launcher:

```text
start.bat
```

It will:

- start the proxy with the local `.venv`
- load settings from `.env`
- prompt for an API key if one is not already available in your environment
- warn you if port `8787` is already in use

## Dashboard and stats

- `/dashboard`
  - View totals and filter by API source
- `/stats/summary`
  - Overall or per-upstream summary
- `/stats/daily?days=30`
  - Daily totals
- `/stats/models?days=7`
  - Model totals
- `/stats/upstreams?days=7`
  - Per-upstream totals
- `/upstreams`
  - Shows all configured local proxy routes

## Notes

- This tracks only traffic that goes through the local proxy.
- If another machine or tool uses the same key without this proxy, that usage will not appear in the local database.
- The proxy is optimized for JSON-based OpenAI endpoints. Large file uploads are forwarded, but the implementation is focused on text endpoints.
