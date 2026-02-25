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


# ── Metrics ────────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.waiting: int = 0          # requests held before semaphore
        self.active: int = 0           # requests currently running upstream
        self.latencies: deque[float] = deque(maxlen=200)
        self.token_log: deque[tuple[float, int]] = deque(maxlen=2000)  # (mono_ts, count)
        self.total_req: int = 0
        self.total_tokens: int = 0
        self.user_tokens: dict[str, int] = {}

    @property
    def depth(self) -> int:
        return self.waiting + self.active

    def tps(self, window: float = 60.0) -> float:
        now = time.monotonic()
        return round(
            sum(c for t, c in self.token_log if now - t <= window) / window, 2
        )

    def avg_latency(self) -> float | None:
        if not self.latencies:
            return None
        return round(sum(self.latencies) / len(self.latencies), 3)

    async def record(self, elapsed: float, tokens: int, user: str) -> None:
        async with self._lock:
            self.latencies.append(elapsed)
            self.total_req += 1
            if tokens:
                self.token_log.append((time.monotonic(), tokens))
                self.total_tokens += tokens
                self.user_tokens[user] = self.user_tokens.get(user, 0) + tokens


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


def _parse_usage(obj: dict) -> dict:
    """Extract usage dict from both chat/completions and responses API formats."""
    return obj.get("usage") or obj.get("response", {}).get("usage") or {}


def _fmt_usage(usage: dict) -> str:
    """Format a usage dict for logging, showing all token fields present."""
    if not usage:
        return "no usage"
    # Normalise to readable short names; keep any unknown fields as-is
    label = {
        "prompt_tokens": "prompt",
        "input_tokens": "input",
        "completion_tokens": "completion",
        "output_tokens": "output",
        "total_tokens": "total",
    }
    parts = []
    for key, val in usage.items():
        if isinstance(val, (int, float)):
            parts.append(f"{label.get(key, key)}={val}")
    return "  ".join(parts)


def _output_tokens(usage: dict) -> int:
    """Return the output/completion token count for metrics tracking."""
    return int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)


def _extract_usage_from_chunk(chunk: bytes) -> dict:
    """Parse an SSE chunk and return the usage dict if present (empty dict otherwise)."""
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
    # SSE comment keeps OpenAI-SDK clients happy; data event is readable by custom code
    yield f": queue-position={q_pos}\ndata: {queue_msg}\n\n".encode()

    # Wait for our turn (honour client disconnect/cancellation)
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
    usage: dict = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream(method, upstream, headers=headers, content=body) as resp:
                async for chunk in resp.aiter_bytes():
                    chunk_usage = _extract_usage_from_chunk(chunk)
                    if chunk_usage:
                        usage = chunk_usage
                    yield chunk
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        err = json.dumps({"error": {"message": str(exc), "type": "gateway_error"}})
        yield f"data: {err}\n\ndata: [DONE]\n\n".encode()
    finally:
        _sem.release()
        elapsed = time.monotonic() - start
        tokens = _output_tokens(usage)
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, tokens, user)
        log.info("[%s] %s /v1/%s → stream done  %s  %.3fs",
                 user, method, path, _fmt_usage(usage), elapsed)


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
        q_pos = M.depth  # position includes self

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
    tokens = 0
    usage: dict = {}
    status_code: int | str = "err"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.request(request.method, upstream, headers=fwd, content=body)
        status_code = resp.status_code
        try:
            usage = _parse_usage(resp.json())
            tokens = _output_tokens(usage)
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
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, tokens, user)
        log.info("[%s] %s /v1/%s → %s  %s  %.3fs",
                 user, request.method, path, status_code, _fmt_usage(usage), elapsed)


# ── Metrics (no auth) ──────────────────────────────────────────────────────────

@app.get("/metrics")
async def metrics() -> dict:
    return {
        "queue": {
            "waiting":  M.waiting,
            "active":   M.active,
            "depth":    M.depth,
        },
        "throughput": {
            "tokens_per_second_1m":  M.tps(60),
            "tokens_per_second_5m":  M.tps(300),
        },
        "latency": {
            "avg_response_time_s": M.avg_latency(),
        },
        "totals": {
            "requests": M.total_req,
            "tokens":   M.total_tokens,
        },
        "per_user_tokens": M.user_tokens,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "upstream": UPSTREAM_URL}
