"""Prometheus HTTP API client (plus optional kubectl port-forward bootstrap)."""
import atexit
import shutil
import subprocess
import time
from urllib.parse import urlparse

import requests


class PrometheusError(RuntimeError):
    pass


RECONNECT_EVERY_S = 15     # at most one attempt to restore the connection per this many seconds


class Prometheus:
    def __init__(self, url: str, restore=None):
        self.url = url.rstrip("/")
        self.http = requests.Session()
        self.clock_offset = 0.0  # cluster clock - local clock, seconds
        self.restore = restore   # re-establishes the connection (the port-forward), if this client made one
        self._last_restore = 0.0

    def _restored(self) -> bool:
        """After the cluster restarts, the port-forward behind `url` is gone: start it again (rate-limited)."""
        if self.restore is None or time.time() - self._last_restore < RECONNECT_EVERY_S:
            return False
        self._last_restore = time.time()
        return self.restore()

    # -- connectivity -------------------------------------------------------
    def ready(self) -> bool:
        try:
            return self.http.get(f"{self.url}/-/ready", timeout=2).ok
        except requests.RequestException:
            return False

    def sync_clock(self) -> float:
        """Docker/WSL VMs drift from the host clock; align 'now' with the cluster's clock."""
        t_local = time.time()
        data = self._get("query", {"query": "time()"})
        self.clock_offset = float(data["result"][0]) - t_local
        return self.clock_offset

    def now(self) -> float:
        return time.time() + self.clock_offset

    # -- queries ------------------------------------------------------------
    def _get(self, endpoint: str, params: dict) -> dict:
        try:
            r = self.http.get(f"{self.url}/api/v1/{endpoint}", params=params, timeout=15)
        except requests.RequestException as exc:
            if not self._restored():
                raise PrometheusError(f"Prometheus unreachable at {self.url}: {exc}") from exc
            try:
                r = self.http.get(f"{self.url}/api/v1/{endpoint}", params=params, timeout=15)
            except requests.RequestException as again:
                raise PrometheusError(f"Prometheus unreachable at {self.url}: {again}") from again
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or body.get("status") != "success":
            raise PrometheusError(f"query failed ({r.status_code}): {body.get('error', r.text[:300])}")
        return body["data"]

    def query(self, promql: str, at: float | None = None) -> list[dict]:
        params = {"query": promql}
        if at is not None:
            params["time"] = at
        data = self._get("query", params)
        if data["resultType"] != "vector":
            return []
        return [{"labels": s["metric"], "value": float(s["value"][1])} for s in data["result"]]

    def query_range(self, promql: str, start: float, end: float, step: float = 5) -> list[dict]:
        data = self._get("query_range", {"query": promql, "start": start, "end": end, "step": step})
        out = []
        for s in data["result"]:
            pts = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
            out.append({"labels": s["metric"], "values": pts})
        return out

    def scalar(self, promql: str, default: float | None = None) -> float | None:
        res = self.query(promql)
        return res[0]["value"] if res else default


def connect(settings) -> Prometheus:
    """Return a Prometheus client, starting `kubectl port-forward` if needed. A client that started one restarts it
    when Prometheus becomes unreachable (the forward dies when the cluster restarts)."""
    prom = Prometheus(settings.prometheus_url)
    procs: list = []

    def port_forward() -> bool:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        port = urlparse(settings.prometheus_url).port or 9090
        kubectl = shutil.which("kubectl") or "kubectl"
        cmd = [kubectl]
        if settings.kube_context:
            cmd += ["--context", settings.kube_context]
        cmd += ["-n", settings.prometheus_namespace, "port-forward",
                f"svc/{settings.prometheus_service}", f"{port}:9090"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        atexit.register(proc.terminate)
        for _ in range(40):
            if prom.ready():
                return True
            time.sleep(0.5)
        return False

    if settings.auto_port_forward:
        prom.restore = port_forward        # also when another process's forward is serving now: it can die too
        if not prom.ready():
            port_forward()
    if not prom.ready():
        raise PrometheusError(
            f"Prometheus not reachable at {settings.prometheus_url}. Is the monitoring stack deployed?"
        )
    prom.sync_clock()
    return prom
