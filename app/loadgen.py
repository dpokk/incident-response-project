"""Open-loop traffic generator simulating users.

Sends a steady baseline request rate to the frontend and exposes a small control API to
trigger a traffic spike (ramp up -> hold -> ramp down). Open-loop means it keeps sending at
the target rate regardless of how slowly the system responds, which is how real user
traffic behaves.

Control API (port 8089):
  GET  /status
  POST /spike  {"peak_rps": 800, "ramp_s": 10, "hold_s": 90, "rampdown_s": 5}
  POST /stop   cancel an active spike
  POST /base   {"rps": 100}
"""
import asyncio
import os
import random
import time
from collections import Counter

import aiohttp
from aiohttp import web

from common import log

TARGET_URL = os.getenv("TARGET_URL", "http://frontend.shop.svc.cluster.local:8080/api/orders")
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "8089"))
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "5"))
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "5000"))
TICK_S = 0.01


class State:
    base_rps = float(os.getenv("BASE_RPS", "100"))
    spike = None  # dict with start, ramp_s, hold_s, rampdown_s, peak_rps
    inflight = 0
    outcomes: Counter = Counter()


def target_rps(now: float) -> float:
    s = State.spike
    if not s:
        return State.base_rps
    dt = now - s["start"]
    base, peak = State.base_rps, s["peak_rps"]
    if dt < s["ramp_s"]:
        return base + (peak - base) * dt / max(s["ramp_s"], 1e-6)
    dt -= s["ramp_s"]
    if dt < s["hold_s"]:
        return peak
    dt -= s["hold_s"]
    if dt < s["rampdown_s"]:
        return peak - (peak - base) * dt / max(s["rampdown_s"], 1e-6)
    State.spike = None
    log("info", "traffic spike finished; back to baseline", base_rps=base)
    return base


async def one_request(session: aiohttp.ClientSession) -> None:
    State.inflight += 1
    quantity = random.randint(1, 5)
    params = {"quantity": quantity, "total": round(quantity * random.uniform(5, 50), 2)}
    try:
        async with session.get(TARGET_URL, params=params) as resp:
            await resp.read()
            State.outcomes["ok" if resp.status < 500 else f"http_{resp.status}"] += 1
    except asyncio.TimeoutError:
        State.outcomes["timeout"] += 1
    except aiohttp.ClientError:
        State.outcomes["connection_error"] += 1
    finally:
        State.inflight -= 1


async def generator(app: web.Application) -> None:
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=10),
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
    )
    app["session"] = session
    credit, last = 0.0, time.monotonic()
    while True:
        await asyncio.sleep(TICK_S)
        now = time.monotonic()
        credit += target_rps(now) * (now - last)
        last = now
        n = int(credit)
        credit -= n
        for _ in range(n):
            if State.inflight >= MAX_INFLIGHT:
                State.outcomes["skipped_client_saturated"] += 1
                continue
            asyncio.create_task(one_request(session))


async def reporter(_: web.Application) -> None:
    while True:
        await asyncio.sleep(10)
        log("info", "load status", target_rps=round(target_rps(time.monotonic()), 1),
            inflight=State.inflight, outcomes_10s=dict(State.outcomes))
        State.outcomes.clear()


async def status(_: web.Request) -> web.Response:
    return web.json_response({
        "target_url": TARGET_URL,
        "base_rps": State.base_rps,
        "current_target_rps": round(target_rps(time.monotonic()), 1),
        "spike_active": State.spike is not None,
        "inflight": State.inflight,
    })


async def spike(request: web.Request) -> web.Response:
    body = await request.json() if request.can_read_body else {}
    State.spike = {
        "start": time.monotonic(),
        "peak_rps": float(body.get("peak_rps", 800)),
        "ramp_s": float(body.get("ramp_s", 10)),
        "hold_s": float(body.get("hold_s", 90)),
        "rampdown_s": float(body.get("rampdown_s", 5)),
    }
    log("info", "traffic spike started", **{k: v for k, v in State.spike.items() if k != "start"})
    return web.json_response({"ok": True, "spike": {k: v for k, v in State.spike.items() if k != "start"}})


async def stop(_: web.Request) -> web.Response:
    State.spike = None
    log("info", "traffic spike cancelled")
    return web.json_response({"ok": True})


async def set_base(request: web.Request) -> web.Response:
    body = await request.json()
    State.base_rps = float(body["rps"])
    log("info", "baseline rate changed", base_rps=State.base_rps)
    return web.json_response({"ok": True, "base_rps": State.base_rps})


async def on_startup(app: web.Application) -> None:
    log("info", "load generator starting", target_url=TARGET_URL, base_rps=State.base_rps)
    app["tasks"] = [asyncio.create_task(generator(app)), asyncio.create_task(reporter(app))]


def main() -> None:
    app = web.Application()
    app.router.add_get("/status", status)
    app.router.add_post("/spike", spike)
    app.router.add_post("/stop", stop)
    app.router.add_post("/base", set_base)
    app.on_startup.append(on_startup)
    web.run_app(app, port=CONTROL_PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
