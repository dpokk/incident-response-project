"""HTTP API + Server-Sent Events for the demo page. Thin: every route calls the engine, which does the real work."""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

WEB = Path(__file__).resolve().parent / "web"


def create_app(engine, bus) -> FastAPI:
    app = FastAPI(title="Incident Response Demo")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/snapshot")
    def snapshot():
        return JSONResponse(json.loads(json.dumps(engine.snapshot(), default=str)))

    @app.get("/api/events")
    async def events():
        q, history = bus.subscribe()

        async def stream():
            try:
                for ev in history:
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(q)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/start")
    def start():
        engine.start()
        return {"ok": True}

    @app.post("/api/incidents/{key}/trigger")
    def trigger(key: str):
        return engine.trigger(key)

    @app.post("/api/reset")
    def reset():
        return engine.reset()

    @app.post("/api/auto")
    def auto(body: dict):
        """Switch automatic remediation on/off (a setting; no incident decision is ever taken from the page)."""
        return engine.set_auto(bool(body.get("enabled")))

    return app
