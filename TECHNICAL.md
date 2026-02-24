# Technical Reference

Deep-dive into the internals of llm-gateway.

## Stack

| Layer | Technology |
| ----- | ---------- |
| Gateway | Python 3.12, FastAPI 0.115, Uvicorn 0.32 |
| HTTP client | httpx 0.28 (async) |
| Container runtime | Docker Compose |
| External ingress | Cloudflare Tunnel (`cloudflared`) |

---

## Request lifecycle

```
Client
  │
  │  Authorization: Bearer sk-xxx
  ▼
FastAPI :8000
  │
  ├─ get_user()         read users.json, validate key → username
  │
  ├─ M._lock            increment M.waiting, snapshot queue depth (q_pos)
  │
  ├─ is_stream?
  │    │
  │    ├─ YES → StreamingResponse(_stream(...))
  │    │          │
  │    │          ├─ yield queue SSE event immediately (client sees position)
  │    │          ├─ await _sem.acquire()           ← blocks here if at capacity
  │    │          ├─ M.waiting--, M.active++
  │    │          ├─ httpx.AsyncClient.stream()     ← proxy SSE chunks
  │    │          ├─ _extract_completion_tokens()   ← scan each chunk for usage
  │    │          └─ finally: _sem.release(), M.active--, M.record()
  │    │
  │    └─ NO  → await _sem.acquire()
  │               ├─ M.waiting--, M.active++
  │               ├─ httpx.AsyncClient.request()   ← full response
  │               ├─ parse usage.completion_tokens from JSON
  │               └─ finally: _sem.release(), M.active--, M.record()
  │
  └─ Response → Client
```

---

## Concurrency model

`MAX_CONCURRENT` controls an `asyncio.Semaphore` initialised at app startup (lifespan). All async coroutines run in a single event loop — no threads, no multiprocessing.

- `M.waiting` — requests that have entered the queue but have not yet acquired the semaphore
- `M.active` — requests currently holding the semaphore and streaming/awaiting upstream
- `M.depth` = `waiting + active` — total queue depth reported to clients

The semaphore is acquired **after** the queue SSE event is yielded for streaming requests, so clients receive feedback instantly even under heavy load.

### Client disconnect handling

Both paths catch `asyncio.CancelledError` on the `await _sem.acquire()` call. If the client disconnects while waiting in queue, `M.waiting` is decremented and the semaphore slot is never consumed.

---

## Auth

`get_user()` is a FastAPI dependency injected into every `/v1/*` route. It calls `_users()` on every request, which opens and reads `users.json` from disk each time. This is intentional: it allows live changes (via `users.sh`) to take effect without restarting the container.

Fallback: if `users.json` is missing, `_users()` falls back to `API_KEY` / `API_USER` environment variables. This allows single-user setups without a file.

---

## Header handling

`_DROP_HEADERS` is a `frozenset` of lowercase header names stripped before forwarding to upstream:

```
host  content-length  authorization  content-encoding
transfer-encoding  connection  keep-alive  te  trailers  upgrade
```

These are either hop-by-hop headers (RFC 7230 §6.1) or headers that must be recomputed for the new request. One header is **added**: `X-Gateway-User: <username>`, allowing the upstream to log or filter by authenticated user.

---

## Metrics

All state lives in the singleton `M = Metrics()`. It resets on container restart.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `waiting` | `int` | Current requests in queue |
| `active` | `int` | Current requests running upstream |
| `latencies` | `deque(maxlen=200)` | Last 200 end-to-end response times (seconds) |
| `token_log` | `deque(maxlen=2000)` | `(monotonic_timestamp, completion_tokens)` pairs |
| `total_req` | `int` | Lifetime request count |
| `total_tokens` | `int` | Lifetime completion token count |
| `user_tokens` | `dict[str, int]` | Per-username lifetime token totals |

**TPS calculation** (`M.tps(window)`): sums `completion_tokens` from `token_log` entries within `window` seconds and divides by `window`. Returns a rolling average, not an instantaneous rate.

**Token counting**:
- Streaming: `_extract_completion_tokens()` scans every SSE chunk for a `data:` line containing `usage.completion_tokens`. llama.cpp emits this in the final chunk before `[DONE]`.
- Non-streaming: reads `response.json()["usage"]["completion_tokens"]` from the completed response body.

All mutations to `M` are protected by `M._lock` (`asyncio.Lock`) to prevent race conditions between concurrent coroutines.

---

## SSE queue event format

For streaming requests, one synthetic SSE event is yielded before the semaphore is acquired:

```
: queue-position=2
data: {"object": "queue.update", "queue_position": 2, "message": "Your query is #2 in the queue"}

```

The `: comment` line keeps SSE connections alive in OpenAI-SDK clients that expect periodic data. The `data:` line is machine-readable for custom clients. The HTTP response also includes `X-Queue-Position: <n>` as a header.

---

## Endpoints

### `GET /health`

No auth. Returns `{"status": "ok", "upstream": "<UPSTREAM_URL>"}`. Intended for Docker healthcheck or uptime monitors.

### `GET /metrics`

No auth. Returns:

```json
{
  "queue":       { "waiting": 0, "active": 1, "depth": 1 },
  "throughput":  { "tokens_per_second_1m": 12.5, "tokens_per_second_5m": 10.2 },
  "latency":     { "avg_response_time_s": 4.312 },
  "totals":      { "requests": 47, "tokens": 18430 },
  "per_user_tokens": { "alice": 12000, "bob": 6430 }
}
```

### `/v1/{path}` (GET, POST, PUT, DELETE, OPTIONS)

Bearer auth required. Path is forwarded verbatim to `UPSTREAM_URL/v1/{path}`. Works for `/v1/chat/completions`, `/v1/models`, `/v1/completions`, etc.

---

## Docker setup

```
gateway/
├── main.py           # entire application (~290 lines)
├── Dockerfile        # python:3.12-slim, uvicorn entrypoint
└── requirements.txt  # fastapi, uvicorn[standard], httpx, python-multipart

docker-compose.yml    # gateway + cloudflared, shared internal network
```

Uvicorn is started with `--proxy-headers` so that `X-Forwarded-For` and `X-Forwarded-Proto` from cloudflared are trusted.

Both services share the `internal` Docker network. `cloudflared` resolves `gateway` by service name — the public hostname in the Cloudflare Zero Trust dashboard must be set to `http://gateway:8000`.

---

## Error responses

| Condition | HTTP status | Body |
| --------- | ----------- | ---- |
| Missing / invalid Bearer token | 401 | `{"detail": "Invalid API key"}` |
| Client disconnect while queued | 499 (non-streaming) / stream closed | — |
| Upstream connect failure | 502 | `{"detail": "Cannot connect to upstream LLM"}` |
| Upstream timeout (>600 s) | 504 | `{"detail": "Upstream timeout"}` |
| Upstream error (streaming) | — | SSE `data: {"error": {...}}` then `data: [DONE]` |

---

## Security notes

- API keys are compared with a plain dict lookup — no timing-safe comparison. Acceptable for a trusted LAN/tunnel deployment; not suitable for internet-exposed services without additional rate limiting.
- `users.json` is mounted read-only into the container (`ro` bind mount). The file is written only by `users.sh` on the host.
- `TUNNEL_TOKEN` is passed as an environment variable, not written to disk inside the container.
- The `/metrics` and `/health` endpoints are unauthenticated by design — they expose no user data beyond aggregate token counts per username.
