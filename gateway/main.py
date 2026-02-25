"""LLM Gateway — HTTPS proxy for llama.cpp with auth, queue tracking, and metrics."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ── Config ─────────────────────────────────────────────────────────────────────

UPSTREAM_URL: str = os.getenv("UPSTREAM_URL", "http://192.168.1.46:8001")
USERS_FILE: str = os.getenv("USERS_FILE", "/app/users.json")
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "1"))

_sem: asyncio.Semaphore  # initialised in lifespan

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gateway")


def _users() -> dict[str, str]:
    """Return {api_key → username} from JSON file, or fall back to env vars."""
    try:
        with open(USERS_FILE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        key = os.getenv("API_KEY", "")
        return {key: os.getenv("API_USER", "admin")} if key else {}


# ── Token normalisation ────────────────────────────────────────────────────────

# Fields that map to canonical names (both chat/completions and responses API)
_KNOWN_USAGE = {
    "prompt_tokens":     "input",
    "input_tokens":      "input",
    "completion_tokens": "output",
    "output_tokens":     "output",
    "total_tokens":      "total",
}


def _normalize_usage(usage: dict) -> dict[str, int]:
    """
    Collapse API-specific field names into canonical names:
      prompt_tokens / input_tokens   → input
      completion_tokens / output_tokens → output
      total_tokens                   → total
    Any unknown numeric field (e.g. reasoning_tokens, cache_tokens) is kept as-is.
    """
    out: dict[str, int] = {}
    for k, v in usage.items():
        if not isinstance(v, (int, float)):
            continue
        canonical = _KNOWN_USAGE.get(k, k)
        # Use the first value seen for aliased keys (don't double-count)
        if canonical not in out:
            out[canonical] = int(v)
    return out


def _parse_usage(obj: dict) -> dict:
    """Extract raw usage dict from both chat/completions and responses API formats."""
    return obj.get("usage") or obj.get("response", {}).get("usage") or {}


def _fmt_usage(usage: dict) -> str:
    """Format raw upstream usage dict for log lines."""
    if not usage:
        return "no usage"
    parts = [f"{k}={v}" for k, v in usage.items() if isinstance(v, (int, float))]
    return "  ".join(parts)


# ── Metrics ────────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.waiting: int = 0          # requests held before semaphore
        self.active: int = 0           # requests currently running upstream
        self.latencies: deque[float] = deque(maxlen=200)
        self.token_log: deque[tuple[float, int]] = deque(maxlen=2000)  # (mono_ts, output_tokens)
        self.total_req: int = 0
        self.totals: dict[str, int] = {}           # global totals per canonical token type
        self.user_tokens: dict[str, dict[str, int]] = {}  # per-user totals per token type

    @property
    def depth(self) -> int:
        return self.waiting + self.active

    def tps(self, window: float = 60.0) -> float:
        """Output tokens per second over the given rolling window."""
        now = time.monotonic()
        return round(
            sum(c for t, c in self.token_log if now - t <= window) / window, 2
        )

    def avg_latency(self) -> float | None:
        if not self.latencies:
            return None
        return round(sum(self.latencies) / len(self.latencies), 3)

    async def record(self, elapsed: float, usage: dict[str, int], user: str) -> None:
        """Record a completed request. usage must already be normalised."""
        async with self._lock:
            self.latencies.append(elapsed)
            self.total_req += 1
            if usage:
                out = usage.get("output", 0)
                if out:
                    self.token_log.append((time.monotonic(), out))
                for k, v in usage.items():
                    self.totals[k] = self.totals.get(k, 0) + v
                bucket = self.user_tokens.setdefault(user, {})
                for k, v in usage.items():
                    bucket[k] = bucket.get(k, 0) + v


M = Metrics()


# ── App / lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _sem
    _sem = asyncio.Semaphore(MAX_CONCURRENT)
    yield


app = FastAPI(title="LLM Gateway", version="1.0", lifespan=lifespan)


# ── Auth ───────────────────────────────────────────────────────────────────────

_http_bearer = HTTPBearer()


async def get_user(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_http_bearer)],
) -> str:
    user = _users().get(creds.credentials)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ── Helpers ────────────────────────────────────────────────────────────────────

_DROP_HEADERS = frozenset(
    "host content-length authorization content-encoding "
    "transfer-encoding connection keep-alive te trailers upgrade".split()
)


def _fwd_headers(req: Request, user: str) -> dict[str, str]:
    return {k: v for k, v in req.headers.items() if k.lower() not in _DROP_HEADERS} | {
        "X-Gateway-User": user
    }


def _extract_usage_from_chunk(chunk: bytes) -> dict:
    """Parse an SSE chunk and return the raw usage dict if present."""
    for line in chunk.decode(errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw in ("[DONE]", ""):
            continue
        try:
            usage = _parse_usage(json.loads(raw))
            if usage:
                return usage
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ── Streaming generator ────────────────────────────────────────────────────────

async def _stream(
    upstream: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
    q_pos: int,
    user: str,
    path: str,
) -> AsyncIterator[bytes]:
    """
    1. Immediately sends queue position to the client (before any wait).
    2. Waits for the concurrency semaphore.
    3. Proxies the upstream SSE stream.
    4. Records metrics on completion.
    """
    queue_msg = json.dumps({
        "object": "queue.update",
        "queue_position": q_pos,
        "message": f"Your query is #{q_pos} in the queue",
    })
    yield f": queue-position={q_pos}\ndata: {queue_msg}\n\n".encode()

    try:
        await _sem.acquire()
    except asyncio.CancelledError:
        async with M._lock:
            M.waiting -= 1
        return

    async with M._lock:
        M.waiting -= 1
        M.active += 1

    start = time.monotonic()
    raw_usage: dict = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream(method, upstream, headers=headers, content=body) as resp:
                async for chunk in resp.aiter_bytes():
                    chunk_usage = _extract_usage_from_chunk(chunk)
                    if chunk_usage:
                        raw_usage = chunk_usage
                    yield chunk
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        err = json.dumps({"error": {"message": str(exc), "type": "gateway_error"}})
        yield f"data: {err}\n\ndata: [DONE]\n\n".encode()
    finally:
        _sem.release()
        elapsed = time.monotonic() - start
        usage = _normalize_usage(raw_usage)
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, usage, user)
        log.info("[%s] %s /v1/%s → stream done  %s  %.3fs",
                 user, method, path, _fmt_usage(raw_usage), elapsed)


# ── Proxy route ────────────────────────────────────────────────────────────────

@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)
async def proxy(
    path: str,
    request: Request,
    user: Annotated[str, Depends(get_user)],
) -> Response:
    body = await request.body()
    upstream = f"{UPSTREAM_URL}/v1/{path}"
    fwd = _fwd_headers(request, user)

    is_stream = False
    if body:
        try:
            is_stream = bool(json.loads(body).get("stream", False))
        except (json.JSONDecodeError, AttributeError):
            pass

    async with M._lock:
        M.waiting += 1
        q_pos = M.depth

    log.info("[%s] %s /v1/%s  q=%d  stream=%s",
             user, request.method, path, q_pos, "yes" if is_stream else "no")

    # ── Streaming ──────────────────────────────────────────────────────────────
    if is_stream:
        return StreamingResponse(
            _stream(upstream, request.method, fwd, body, q_pos, user, path),
            media_type="text/event-stream",
            headers={"X-Queue-Position": str(q_pos)},
        )

    # ── Non-streaming ──────────────────────────────────────────────────────────
    try:
        await _sem.acquire()
    except asyncio.CancelledError:
        async with M._lock:
            M.waiting -= 1
        raise HTTPException(499, "Client disconnected")

    async with M._lock:
        M.waiting -= 1
        M.active += 1

    start = time.monotonic()
    raw_usage: dict = {}
    status_code: int | str = "err"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.request(request.method, upstream, headers=fwd, content=body)
        status_code = resp.status_code
        try:
            raw_usage = _parse_usage(resp.json())
        except Exception:
            pass
        safe = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_HEADERS}
        safe["X-Queue-Position"] = str(q_pos)
        return Response(content=resp.content, status_code=resp.status_code, headers=safe)
    except httpx.TimeoutException:
        status_code = 504
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except httpx.ConnectError:
        status_code = 502
        raise HTTPException(status_code=502, detail="Cannot connect to upstream LLM")
    finally:
        _sem.release()
        elapsed = time.monotonic() - start
        usage = _normalize_usage(raw_usage)
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, usage, user)
        log.info("[%s] %s /v1/%s → %s  %s  %.3fs",
                 user, request.method, path, status_code, _fmt_usage(raw_usage), elapsed)


# ── Metrics (no auth) ──────────────────────────────────────────────────────────

@app.get("/metrics")
async def metrics() -> dict:
    return {
        "queue": {
            "waiting": M.waiting,
            "active":  M.active,
            "depth":   M.depth,
        },
        "throughput": {
            "output_tokens_per_second_1m": M.tps(60),
            "output_tokens_per_second_5m": M.tps(300),
        },
        "latency": {
            "avg_response_time_s": M.avg_latency(),
        },
        "totals": {
            "requests": M.total_req,
            "tokens":   M.totals,
        },
        "per_user_tokens": M.user_tokens,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "upstream": UPSTREAM_URL}
