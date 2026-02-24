# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An HTTPS reverse proxy gateway for llama.cpp (or any OpenAI-compatible LLM backend). Adds bearer token auth, concurrency limiting with queue tracking, and per-user token metrics on top of a bare llama.cpp server.

## Installation (one-command)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/hcastc00/llm-gateway/main/install.sh)
```

The installer handles Docker installation, interactive config, Cloudflare tunnel setup, and service startup.

**Before publishing to GitHub:** update the `REPO_URL` variable at the top of [install.sh](install.sh).

## Running Manually

**Start everything:**

```bash
docker compose up --build
```

**Run gateway locally without Docker (for development):**

```bash
pip install -r gateway/requirements.txt
uvicorn gateway.main:app --reload --port 8000
```

## Configuration

All config comes from environment variables (set in `.env`). See [.env.example](.env.example) for the template.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `UPSTREAM_URL` | `http://192.168.1.46:8001` | Target llama.cpp backend |
| `MAX_CONCURRENT` | `1` | Max simultaneous requests to upstream |
| `TUNNEL_TOKEN` | — | Cloudflare tunnel token |
| `USERS_FILE` | `/app/users.json` | Path to API key→username JSON |

**`users.json` format:**

```json
{"sk-abc123": "alice", "sk-xyz789": "bob"}
```

## Architecture

```text
Cloudflare Edge → cloudflared container → FastAPI :8000 → Upstream LLM :8001
                                          (also on LAN at host-ip:8000)
```

- **cloudflared** connects outbound to Cloudflare — no open router ports needed. Configure the public hostname in the Cloudflare Zero Trust dashboard: `Service: http://gateway:8000`.
- **`gateway/main.py`** is the entire Python application — a single-file FastAPI app.
- **Port 8000** is also exposed directly on the host for LAN access (no TLS on LAN).

### Key Flows in `gateway/main.py`

**Auth** (`get_user`): Bearer token is looked up in `users.json` on every request (file read per request — intentional for live reload of users).

**Proxy route** (`/v1/{path}`): Inspects request body for `"stream": true` to decide between streaming and non-streaming paths. Both paths use `asyncio.Semaphore` for concurrency control.

**Queue tracking**: Before acquiring the semaphore, `M.waiting` is incremented and the queue position is computed. For streaming requests, a synthetic SSE event is sent immediately so clients see their queue position before waiting.

**Streaming** (`_stream` generator): Sends queue update → acquires semaphore → proxies SSE chunks → parses `completion_tokens` from the final usage chunk → releases semaphore and records metrics.

**Metrics** (`M: Metrics`): In-memory only, resets on restart. Rolling windows use `deque(maxlen=...)`. Token counts come from parsing `usage.completion_tokens` in SSE chunks or JSON responses.

### Endpoints

| Endpoint | Auth | Purpose |
| -------- | ---- | ------- |
| `GET /health` | No | Liveness check |
| `GET /metrics` | No | Queue depth, TPS, latency, per-user token totals |
| `/v1/*` | Bearer | Proxied to upstream |

### Header Handling

`_DROP_HEADERS` strips `host`, `authorization`, `content-length`, `content-encoding`, `transfer-encoding`, and hop-by-hop headers before forwarding. Adds `X-Gateway-User: <username>` to upstream requests.
