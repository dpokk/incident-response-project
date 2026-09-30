"""Backend "orders" API, backed by PostgreSQL.

Request path (GET /api/orders?quantity=N&total=X):
  allocate a working buffer -> fixed CPU work on a small worker pool -> INSERT the order into PostgreSQL.

Background "invoice" job: every second it takes un-invoiced orders and computes the unit price.

Failure behaviour (deliberately realistic, never driven by a scenario flag):
  * Requests arriving faster than the CPU limit allows queue up while holding their buffers, so memory
    grows with the backlog and the container can be OOMKilled (no load shedding).
  * If PostgreSQL can't be reached, requests fail fast with 503 and the error is logged; the process keeps
    running and reconnects when the database comes back.
  * The invoice job does not validate quantity. An order with quantity=0 raises ZeroDivisionError. The job
    is fail-fast by design: an unexpected exception terminates the process, and because the bad order is
    still pending after the restart, the pod crash-loops.
"""
import asyncio
import hashlib
import os
import queue
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from common import cgroup_memory, log, rss_bytes

PORT = int(os.getenv("PORT", "8080"))
CPU_WORK_MS = float(os.getenv("CPU_WORK_MS", "3"))
BUFFER_KB = int(os.getenv("REQUEST_BUFFER_KB", "256"))
WORKERS = int(os.getenv("WORKER_THREADS", "2"))
READY_MAX_INFLIGHT = int(os.getenv("READY_MAX_INFLIGHT", "300"))
BACKLOG_WARN = int(os.getenv("BACKLOG_WARN_INFLIGHT", "100"))
MEM_WARN_RATIO = float(os.getenv("MEMORY_WARN_RATIO", "0.8"))
STATS_INTERVAL_S = float(os.getenv("STATS_INTERVAL_S", "2"))
SLOW_REQUEST_S = float(os.getenv("SLOW_REQUEST_S", "1"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shop@postgres:5432/shop")
DB_PASSWORD = os.getenv("PGPASSWORD", "")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "4"))
DB_CONNECT_TIMEOUT_S = int(os.getenv("DB_CONNECT_TIMEOUT_S", "3"))
DB_RETRY_BACKOFF_S = float(os.getenv("DB_RETRY_BACKOFF_S", "2"))
INVOICE_INTERVAL_S = float(os.getenv("INVOICE_INTERVAL_S", "1"))

REQUESTS = Counter("http_requests", "HTTP requests handled", ["service", "route", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
)
INFLIGHT = Gauge("backend_inflight_requests", "Requests accepted but not yet completed")
DB_ERRORS = Counter("backend_db_errors", "Database operations that failed", ["kind"])

POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="worker")
JOB_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="invoice")
_BLOCK = b"x" * 1024


# --------------------------------------------------------------------------- database access

class DatabaseUnavailable(Exception):
    """Raised instead of attempting a connection while the database is known to be unreachable."""


def _db_target() -> tuple[str, str]:
    try:
        info = psycopg.conninfo.conninfo_to_dict(DATABASE_URL)
        return info.get("host", "?"), str(info.get("port", "5432"))
    except psycopg.ProgrammingError:
        return "?", "?"


class Database:
    """Tiny connection pool with a retry backoff so a dead database makes requests fail fast."""

    def __init__(self, url: str, password: str, size: int):
        self.url, self.password = url, password
        self.idle: queue.LifoQueue = queue.LifoQueue()
        self.slots = threading.BoundedSemaphore(size)
        self.retry_at = 0.0
        self.last_error = ""
        self.schema_ready = False
        self.lock = threading.Lock()
        self.probe = threading.Lock()
        self.host, self.port = _db_target()
        self.state = "unknown"

    def _connect(self):
        try:
            conn = psycopg.connect(self.url, password=self.password or None, connect_timeout=DB_CONNECT_TIMEOUT_S,
                                   application_name=os.getenv("POD_NAME", "backend"))
        except psycopg.OperationalError as exc:
            self._failed(exc, "connect")
            raise
        if not self.schema_ready:
            with self.lock:
                if not self.schema_ready:
                    with conn.cursor() as cur:
                        cur.execute("""
                            CREATE TABLE IF NOT EXISTS orders (
                                id BIGSERIAL PRIMARY KEY,
                                order_ref UUID NOT NULL,
                                quantity INTEGER NOT NULL,
                                total NUMERIC(12,2) NOT NULL,
                                unit_price NUMERIC(12,2),
                                invoiced BOOLEAN NOT NULL DEFAULT FALSE,
                                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
                        cur.execute("CREATE INDEX IF NOT EXISTS orders_pending ON orders (id) WHERE NOT invoiced")
                    conn.commit()
                    self.schema_ready = True
        if self.state != "connected":
            log("info", "database connection established", db_host=self.host, db_port=self.port)
            self.state = "connected"
        return conn

    def _failed(self, exc: Exception, kind: str) -> None:
        DB_ERRORS.labels(kind).inc()
        self.retry_at = time.monotonic() + DB_RETRY_BACKOFF_S
        self.last_error = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        self.schema_ready = False
        self.state = "failing"
        log("error", "database connection failed", db_host=self.host, db_port=self.port, operation=kind,
            error_type=type(exc).__name__, error=str(exc).strip()[:500])

    @contextmanager
    def connection(self):
        if time.monotonic() < self.retry_at:
            raise DatabaseUnavailable(self.last_error)
        # Half-open: while the database is failing, only one caller at a time tries to reconnect;
        # everyone else fails fast instead of blocking a worker on a slow DNS lookup or connect.
        probing = False
        if self.state == "failing":
            if not self.probe.acquire(blocking=False):
                raise DatabaseUnavailable(self.last_error)
            probing = True
        if not self.slots.acquire(timeout=2):
            if probing:
                self.probe.release()
            raise DatabaseUnavailable("connection pool exhausted")
        conn = None
        try:
            try:
                conn = self.idle.get_nowait()
            except queue.Empty:
                try:
                    conn = self._connect()
                finally:
                    if probing:
                        self.probe.release()
                        probing = False
            try:
                yield conn
                conn.commit()
            except psycopg.OperationalError as exc:
                conn.close()
                conn = None
                self._failed(exc, "query")
                raise
            except Exception:
                if not conn.closed:
                    conn.rollback()
                raise
        finally:
            if probing:
                self.probe.release()
            if conn is not None and not conn.closed:
                self.idle.put(conn)
            self.slots.release()


DB = Database(DATABASE_URL, DB_PASSWORD, DB_POOL_SIZE)


# --------------------------------------------------------------------------- request handling

def _spin(iterations: int) -> str:
    h = hashlib.sha256()
    for _ in range(iterations):
        h.update(_BLOCK)
    return h.hexdigest()


def _calibrate() -> float:
    """Iterations per CPU-millisecond, measured with thread CPU time so throttling doesn't skew it."""
    best = 0.0
    for _ in range(5):
        n = 2000
        t0 = time.thread_time()
        _spin(n)
        dt = time.thread_time() - t0
        if dt > 0:
            best = max(best, n / (dt * 1000))
    return best or 300.0


ITERATIONS = 0


class Stats:
    inflight = 0
    completed = 0
    slow = 0
    errors = 0
    db_failed_requests = 0
    not_ready = False


def process_order(buf: bytearray, quantity: int, total: float) -> str:
    buf[0] ^= 1
    buf[-1] ^= 1
    digest = _spin(ITERATIONS)
    ref = str(uuid.uuid4())
    try:
        with DB.connection() as conn:
            conn.execute("INSERT INTO orders (order_ref, quantity, total) VALUES (%s, %s, %s)", (ref, quantity, total))
    except psycopg.OperationalError as exc:
        raise DatabaseUnavailable(str(exc).strip().splitlines()[0]) from exc
    return ref + ":" + digest[:12]


async def orders(request: web.Request) -> web.Response:
    t0 = time.perf_counter()
    Stats.inflight += 1
    INFLIGHT.inc()
    code = 200
    try:
        quantity = int(request.query.get("quantity", "1"))
        total = float(request.query.get("total", "10"))
        buf = bytearray(b"\x5a") * (BUFFER_KB * 1024)
        ref = await asyncio.get_running_loop().run_in_executor(POOL, process_order, buf, quantity, total)
        del buf
        return web.json_response({"order_ref": ref.split(":")[0], "status": "accepted"})
    except DatabaseUnavailable:
        code = 503
        Stats.db_failed_requests += 1
        return web.json_response({"error": "database unavailable"}, status=503)
    except ValueError:
        code = 400
        return web.json_response({"error": "invalid quantity/total"}, status=400)
    except Exception as exc:  # noqa: BLE001
        code = 500
        Stats.errors += 1
        log("error", "order processing failed", error_type=type(exc).__name__, error=str(exc)[:300])
        return web.json_response({"error": "internal error"}, status=500)
    finally:
        elapsed = time.perf_counter() - t0
        Stats.inflight -= 1
        Stats.completed += 1
        INFLIGHT.dec()
        REQUESTS.labels("backend", "/api/orders", str(code)).inc()
        LATENCY.labels("backend", "/api/orders").observe(elapsed)
        if elapsed > SLOW_REQUEST_S:
            Stats.slow += 1


async def healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def readyz(_: web.Request) -> web.Response:
    overloaded = Stats.inflight > READY_MAX_INFLIGHT
    if overloaded != Stats.not_ready:
        Stats.not_ready = overloaded
        if overloaded:
            log("warning", "readiness check failing: request backlog above threshold",
                inflight=Stats.inflight, threshold=READY_MAX_INFLIGHT)
        else:
            log("info", "readiness check passing again", inflight=Stats.inflight)
    if overloaded:
        return web.Response(status=503, text="overloaded")
    return web.Response(text="ready")


async def metrics(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


# --------------------------------------------------------------------------- background work

def invoice_batch() -> int:
    """Compute unit prices for pending orders. Database outages are expected and handled; bugs are not."""
    try:
        with DB.connection() as conn:
            rows = conn.execute(
                "SELECT id, quantity, total FROM orders WHERE NOT invoiced ORDER BY id LIMIT 500 "
                "FOR UPDATE SKIP LOCKED").fetchall()
            updates = []
            for order_id, quantity, total in rows:
                unit_price = total / quantity
                updates.append((unit_price, order_id))
            if updates:
                with conn.cursor() as cur:
                    cur.executemany("UPDATE orders SET unit_price = %s, invoiced = TRUE WHERE id = %s", updates)
            return len(updates)
    except (DatabaseUnavailable, psycopg.OperationalError):
        return 0


async def invoice_worker() -> None:
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(JOB_POOL, invoice_batch)
        await asyncio.sleep(INVOICE_INTERVAL_S)


async def stats_loop() -> None:
    last_completed, last_t = 0, time.monotonic()
    warned_backlog = False
    while True:
        await asyncio.sleep(STATS_INTERVAL_S)
        now = time.monotonic()
        rps = (Stats.completed - last_completed) / (now - last_t)
        last_completed, last_t = Stats.completed, now
        usage, limit = cgroup_memory()
        ratio = (usage / limit) if usage and limit else None
        fields = dict(
            rps=round(rps, 1), inflight=Stats.inflight, slow_requests=Stats.slow, errors=Stats.errors,
            db_state=DB.state, db_failed_requests=Stats.db_failed_requests,
            rss_mb=round((rss_bytes() or 0) / 2**20, 1),
            cgroup_mem_mb=round((usage or 0) / 2**20, 1),
            cgroup_limit_mb=round(limit / 2**20, 1) if limit else None,
            mem_limit_ratio=round(ratio, 3) if ratio is not None else None,
        )
        if Stats.db_failed_requests:
            log("error", "requests failed: database unavailable", count=Stats.db_failed_requests,
                interval_s=STATS_INTERVAL_S, db_host=DB.host, db_port=DB.port, last_error=DB.last_error)
        Stats.slow = Stats.errors = Stats.db_failed_requests = 0
        log("info", "stats", **fields)
        if Stats.inflight >= BACKLOG_WARN:
            log("warning", "request backlog growing; workers cannot keep up", **fields)
            warned_backlog = True
        elif warned_backlog:
            log("info", "request backlog drained", **fields)
            warned_backlog = False
        if ratio is not None and ratio >= MEM_WARN_RATIO:
            log("warning", "memory usage approaching container limit", **fields)


# --------------------------------------------------------------------------- process lifecycle

async def serve() -> None:
    global ITERATIONS
    ITERATIONS = max(1, int(_calibrate() * CPU_WORK_MS))
    _, limit = cgroup_memory()
    log("info", "backend starting", port=PORT, cpu_work_ms=CPU_WORK_MS, buffer_kb=BUFFER_KB,
        worker_threads=WORKERS, iterations_per_request=ITERATIONS, db_host=DB.host, db_port=DB.port,
        memory_limit_mb=round(limit / 2**20, 1) if limit else None)

    app = web.Application()
    app.router.add_get("/api/orders", orders)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, port=PORT, backlog=1024).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stats_task = asyncio.create_task(stats_loop())
    worker = asyncio.create_task(invoice_worker())
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({worker, stopper}, return_when=asyncio.FIRST_COMPLETED)
    stats_task.cancel()
    if worker.done():
        worker.result()  # fail-fast: re-raise the background job's exception and let the process die
    log("warning", "backend shutting down (SIGTERM received)", inflight=Stats.inflight)
    worker.cancel()
    await runner.cleanup()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
