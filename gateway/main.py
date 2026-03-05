"""LLM Gateway — HTTPS proxy for llama.cpp with auth, queue tracking, and metrics."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ── Config ─────────────────────────────────────────────────────────────────────

UPSTREAM_URL: str = os.getenv("UPSTREAM_URL", "http://192.168.1.46:8001")
USERS_FILE: str = os.getenv("USERS_FILE", "/app/users.json")
METRICS_FILE: str = os.getenv("METRICS_FILE", "/app/data/metrics.json")
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "1"))

_sem: asyncio.Semaphore  # initialised in lifespan

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gateway")


class _SuppressPollingPaths(logging.Filter):
    """Drop uvicorn access-log lines for /health and /metrics at INFO level."""

    _PATHS = ("/health ", "/metrics ")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno <= logging.INFO:
            msg = record.getMessage()
            if any(p in msg for p in self._PATHS):
                return False
        return True


# Applied in lifespan after uvicorn configures its own loggers
_suppress_polling = _SuppressPollingPaths()


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
    """Format normalised usage dict for log lines."""
    if not usage:
        return "no usage"
    parts = [f"{k}={v}" for k, v in usage.items()]
    return " ".join(parts)


def _fmt_timings(timings: dict, queue_wait: float) -> str:
    """Format upstream timings + queue wait for log lines."""
    parts = [f"queue={queue_wait:.1f}s"]
    gen_ms = timings.get("predicted_ms")
    if gen_ms is not None:
        parts.append(f"gen={gen_ms / 1000:.1f}s")
    tps = timings.get("predicted_per_second")
    if tps is not None:
        parts.append(f"tps={tps:.2f}")
    prompt_ms = timings.get("prompt_ms")
    if prompt_ms is not None:
        parts.append(f"prompt={prompt_ms / 1000:.1f}s")
    ptps = timings.get("prompt_per_second")
    if ptps is not None:
        parts.append(f"prompt_tps={ptps:.1f}")
    return " ".join(parts)


# ── Metrics ────────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.waiting: int = 0          # requests held before semaphore
        self.active: int = 0           # requests currently running upstream
        self.total_req: int = 0
        self.totals: dict[str, int] = {}           # global totals per canonical token type
        self.user_tokens: dict[str, dict[str, int]] = {}  # per-user totals per token type

    async def record(
        self,
        elapsed: float,
        queue_wait: float,
        usage: dict[str, int],
        timings: dict,
        user: str,
    ) -> None:
        """Record a completed request. usage must already be normalised."""
        async with self._lock:
            self.total_req += 1
            if usage:
                for k, v in usage.items():
                    self.totals[k] = self.totals.get(k, 0) + v
                bucket = self.user_tokens.setdefault(user, {})
                for k, v in usage.items():
                    bucket[k] = bucket.get(k, 0) + v
        # prometheus_client counters/histograms are thread-safe; update outside lock
        _prom_requests.labels(user=user).inc()
        _prom_latency.observe(elapsed)
        _prom_queue_wait.observe(queue_wait)
        for k, v in usage.items():
            _prom_tokens.labels(user=user, token_type=k).inc(v)
        # Record upstream timings when available
        gen_ms = timings.get("predicted_ms")
        if gen_ms is not None:
            _prom_generation.observe(gen_ms / 1000.0)
        prompt_ms = timings.get("prompt_ms")
        if prompt_ms is not None:
            _prom_prompt_eval.observe(prompt_ms / 1000.0)
        tps = timings.get("predicted_per_second")
        if tps is not None:
            _prom_tps.observe(tps)
        ptps = timings.get("prompt_per_second")
        if ptps is not None:
            _prom_prompt_tps.observe(ptps)

    def load(self, path: str) -> None:
        try:
            with open(path) as f:
                data = json.load(f)
            self.total_req = int(data.get("total_req", 0))
            self.totals = data.get("totals", {})
            self.user_tokens = data.get("user_tokens", {})
            log.info("Metrics loaded from %s (requests=%d)", path, self.total_req)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("Could not load metrics from %s: %s", path, exc)

    def save(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "total_req":   self.total_req,
                    "totals":      self.totals,
                    "user_tokens": self.user_tokens,
                }, f)
            os.replace(tmp, path)
        except Exception as exc:
            log.warning("Could not save metrics to %s: %s", path, exc)


M = Metrics()

# ── Prometheus metrics ─────────────────────────────────────────────────────────

_prom_requests = Counter(
    "llm_gateway_requests_total",
    "Total completed requests",
    ["user"],
)
_prom_tokens = Counter(
    "llm_gateway_tokens_total",
    "Total tokens processed",
    ["user", "token_type"],
)
_prom_latency = Histogram(
    "llm_gateway_request_duration_seconds",
    "End-to-end request latency including queue wait",
    buckets=[1, 5, 15, 30, 60, 120, 300, 600],
)
_prom_queue_wait = Histogram(
    "llm_gateway_queue_wait_seconds",
    "Time spent waiting in queue for semaphore",
    buckets=[0.01, 0.1, 0.5, 1, 5, 15, 30, 60, 120],
)
_prom_generation = Histogram(
    "llm_gateway_generation_seconds",
    "Actual generation time (from upstream timings when available)",
    buckets=[1, 5, 15, 30, 60, 120, 300, 600],
)
_prom_prompt_eval = Histogram(
    "llm_gateway_prompt_eval_seconds",
    "Prompt evaluation time from upstream timings",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)
_prom_tps = Histogram(
    "llm_gateway_tokens_per_second",
    "Token generation speed (tokens/s)",
    buckets=[0.5, 1, 2, 5, 10, 20, 50, 100],
)
_prom_prompt_tps = Histogram(
    "llm_gateway_prompt_tokens_per_second",
    "Prompt processing speed (tokens/s)",
    buckets=[1, 5, 10, 20, 50, 100, 200, 500],
)
_prom_queue_waiting = Gauge("llm_gateway_queue_waiting", "Requests waiting for semaphore")
_prom_queue_active = Gauge("llm_gateway_queue_active", "Requests running upstream")
_prom_max_concurrent = Gauge("llm_gateway_max_concurrent", "Configured concurrency limit")


# ── App / lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _sem
    _sem = asyncio.Semaphore(MAX_CONCURRENT)
    _prom_max_concurrent.set(MAX_CONCURRENT)
    M.load(METRICS_FILE)
    logging.getLogger("uvicorn.access").addFilter(_suppress_polling)

    async def _periodic_save() -> None:
        while True:
            await asyncio.sleep(30)
            M.save(METRICS_FILE)

    task = asyncio.create_task(_periodic_save())
    try:
        yield
    finally:
        task.cancel()
        M.save(METRICS_FILE)
        log.info("Metrics saved to %s", METRICS_FILE)


app = FastAPI(title="LLM Gateway", version="1.0", lifespan=lifespan)


# ── Tunnel guard ───────────────────────────────────────────────────────────────

_LOCAL_ONLY_PATHS = frozenset({"/health", "/metrics"})


@app.middleware("http")
async def block_local_only_from_tunnel(request: Request, call_next):
    """Block /health and /metrics when the request comes through the Cloudflare tunnel.
    Cloudflare always injects CF-RAY on tunnel traffic; direct LAN requests won't have it.
    """
    if request.url.path in _LOCAL_ONLY_PATHS and "cf-ray" in request.headers:
        return Response(status_code=404)
    return await call_next(request)


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


def _extract_usage_from_chunk(chunk: bytes) -> tuple[dict, dict]:
    """Parse an SSE chunk and return (raw_usage, timings) if present."""
    usage: dict = {}
    timings: dict = {}
    for line in chunk.decode(errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw in ("[DONE]", ""):
            continue
        try:
            obj = json.loads(raw)
            if not usage:
                u = _parse_usage(obj)
                if u:
                    usage = u
            if not timings:
                t = obj.get("timings") or {}
                if t:
                    timings = t
        except (json.JSONDecodeError, ValueError):
            pass
    return usage, timings


# ── Streaming generator ────────────────────────────────────────────────────────

async def _stream(
    upstream: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
    q_pos: int,
    user: str,
    path: str,
    start: float,
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

    raw_usage: dict = {}
    raw_timings: dict = {}
    gen_start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream(method, upstream, headers=headers, content=body) as resp:
                async for chunk in resp.aiter_bytes():
                    chunk_usage, chunk_timings = _extract_usage_from_chunk(chunk)
                    if chunk_usage:
                        raw_usage = chunk_usage
                    if chunk_timings:
                        raw_timings = chunk_timings
                    yield chunk
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        err = json.dumps({"error": {"message": str(exc), "type": "gateway_error"}})
        yield f"data: {err}\n\ndata: [DONE]\n\n".encode()
    finally:
        _sem.release()
        now = time.monotonic()
        elapsed = now - start
        queue_wait = gen_start - start
        usage = _normalize_usage(raw_usage)
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, queue_wait, usage, raw_timings, user)
        log.info("[%s] %s /v1/%s → stream done  %s  %s  total=%.1fs",
                 user, method, path, _fmt_usage(usage), _fmt_timings(raw_timings, queue_wait), elapsed)


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
    start = time.monotonic()
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
        q_pos = M.waiting + M.active

    log.info("[%s] %s /v1/%s  q=%d  stream=%s",
             user, request.method, path, q_pos, "yes" if is_stream else "no")
    if log.isEnabledFor(logging.DEBUG):
        hdrs = {k: v for k, v in request.headers.items() if k.lower() != "authorization"}
        log.debug("[%s] headers=%s", user, hdrs)

    # ── Streaming ──────────────────────────────────────────────────────────────
    if is_stream:
        return StreamingResponse(
            _stream(upstream, request.method, fwd, body, q_pos, user, path, start),
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

    raw_usage: dict = {}
    raw_timings: dict = {}
    status_code: int | str = "err"
    gen_start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.request(request.method, upstream, headers=fwd, content=body)
        status_code = resp.status_code
        try:
            data = resp.json()
            raw_usage = _parse_usage(data)
            raw_timings = data.get("timings") or {}
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
        now = time.monotonic()
        elapsed = now - start
        queue_wait = gen_start - start
        usage = _normalize_usage(raw_usage)
        async with M._lock:
            M.active -= 1
        await M.record(elapsed, queue_wait, usage, raw_timings, user)
        log.info("[%s] %s /v1/%s → %s  %s  %s  total=%.1fs",
                 user, request.method, path, status_code, _fmt_usage(usage), _fmt_timings(raw_timings, queue_wait), elapsed)


# ── Metrics (no auth) ──────────────────────────────────────────────────────────

@app.get("/metrics")
async def metrics() -> Response:
    _prom_queue_waiting.set(M.waiting)
    _prom_queue_active.set(M.active)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "upstream": UPSTREAM_URL}
