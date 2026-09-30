"""Frontend / API gateway. Receives user traffic and proxies it to the backend service.

This is the edge of the system, so its metrics are what users experience: when the backend
is slow, restarting or has no ready endpoints, the frontend returns 5xx to the caller.
"""
import asyncio
import os
import time
from collections import Counter as TallyCounter

import aiohttp
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from common import log

PORT = int(os.getenv("PORT", "8080"))
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8080").rstrip("/")
UPSTREAM_TIMEOUT_S = float(os.getenv("UPSTREAM_TIMEOUT_S", "3"))
LOG_FLUSH_S = float(os.getenv("ERROR_LOG_FLUSH_S", "2"))

REQUESTS = Counter("http_requests", "HTTP requests served", ["service", "route", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
)
UPSTREAM_ERRORS = Counter("upstream_errors", "Failed calls to upstream services", ["upstream", "reason"])

_error_tally: TallyCounter = TallyCounter()
_error_sample: dict = {}
_served = TallyCounter()


def _record_error(reason: str, detail: str) -> None:
    UPSTREAM_ERRORS.labels("backend", reason).inc()
    _error_tally[reason] += 1
    _error_sample[reason] = detail


async def orders(request: web.Request) -> web.Response:
    t0 = time.perf_counter()
    session: aiohttp.ClientSession = request.app["session"]
    try:
        async with session.get(f"{BACKEND_URL}/api/orders", params=request.query) as resp:
            body = await resp.read()
            code = resp.status
            if code >= 500:
                _record_error(f"upstream_http_{code}", body[:200].decode(errors="replace"))
            out = web.Response(body=body, status=code, content_type="application/json")
    except asyncio.TimeoutError:
        code = 504
        _record_error("upstream_timeout", f"no response from backend within {UPSTREAM_TIMEOUT_S}s")
        out = web.json_response({"error": "upstream timeout"}, status=code)
    except aiohttp.ClientConnectorError as exc:
        code = 503
        _record_error("upstream_connect_error", str(exc))
        out = web.json_response({"error": "backend unavailable"}, status=code)
    except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError, aiohttp.ClientPayloadError) as exc:
        code = 502
        _record_error("upstream_connection_reset", repr(exc))
        out = web.json_response({"error": "bad gateway"}, status=code)
    REQUESTS.labels("frontend", "/api/orders", str(code)).inc()
    LATENCY.labels("frontend", "/api/orders").observe(time.perf_counter() - t0)
    _served["5xx" if code >= 500 else "ok"] += 1
    return out


async def index(_: web.Request) -> web.Response:
    return web.Response(text="shop frontend: GET /api/orders\n")


async def healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


async def log_loop(app: web.Application) -> None:
    """Aggregate upstream errors into periodic log lines instead of one line per failed request."""
    ticks = 0
    while True:
        await asyncio.sleep(LOG_FLUSH_S)
        ticks += 1
        for reason, count in _error_tally.items():
            log("error", "upstream request to backend failed", upstream=BACKEND_URL, reason=reason,
                count=count, interval_s=LOG_FLUSH_S, sample_error=_error_sample.get(reason))
        _error_tally.clear()
        if ticks % 5 == 0:
            total = sum(_served.values())
            log("info", "stats", requests=total, errors_5xx=_served["5xx"],
                error_rate=round(_served["5xx"] / total, 4) if total else 0.0,
                interval_s=LOG_FLUSH_S * 5)
            _served.clear()


async def on_startup(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=10),
        timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S),
    )
    app["log_task"] = asyncio.create_task(log_loop(app))
    log("info", "frontend starting", port=PORT, backend_url=BACKEND_URL, upstream_timeout_s=UPSTREAM_TIMEOUT_S)


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/orders", orders)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", metrics)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=PORT, access_log=None, print=None, backlog=2048)


if __name__ == "__main__":
    main()
