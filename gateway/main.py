"""LLM Gateway — HTTPS proxy for llama.cpp with auth, queue tracking, and metrics."""
from __future__ import annotations

import asyncio
import json
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


def _extract_completion_tokens(chunk: bytes) -> int:
    """Parse an SSE chunk and return completion_tokens (0 if not found)."""
    for line in chunk.decode(errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            continue
        try:
            usage = json.loads(raw).get("usage") or {}
            ct = int(usage.get("completion_tokens", 0))
            if ct:
                return ct
        except (json.JSONDecodeError, ValueError):
            pass
    return 0


# ── Streaming generator ────────────────────────────────────────────────────────

async def _stream(
    upstream: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
    q_pos: int,
    user: str,
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
    tokens = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream(method, upstream, headers=headers, content=body) as resp:
                async for chunk in resp.aiter_bytes():
                    tokens += _extract_completion_tokens(chunk)
                    yield chunk
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        err = json.dumps({"error": {"message": str(exc), "type": "gateway_error"}})
        yield f"data: {err}\n\ndata: [DONE]\n\n".encode()
    finally:
        _sem.release()
        elapsed = time.monotonic() - start
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, tokens, user)


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

    # ── Streaming ──────────────────────────────────────────────────────────────
    if is_stream:
        return StreamingResponse(
            _stream(upstream, request.method, fwd, body, q_pos, user),
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
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.request(request.method, upstream, headers=fwd, content=body)
        try:
            tokens = int((resp.json().get("usage") or {}).get("completion_tokens", 0))
        except Exception:
            pass
        safe = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_HEADERS}
        safe["X-Queue-Position"] = str(q_pos)
        return Response(content=resp.content, status_code=resp.status_code, headers=safe)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot connect to upstream LLM")
    finally:
        _sem.release()
        elapsed = time.monotonic() - start
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, tokens, user)


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
