"""Demo: the timing facts both agents get (so old restarts and changes are not read as the cause), and recovery of
the connections after the cluster restarts. Fake cluster, fake HTTP; no network, no cluster."""
import os
import sys
import time
from types import SimpleNamespace

import requests

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_demo_mirror import engine  # noqa: E402
from test_history import investigate  # noqa: E402
from urllib3.exceptions import MaxRetryError  # noqa: E402

import demo.live as live  # noqa: E402
from demo.agent.investigator import InvestigatorAgent, timing  # noqa: E402
from demo.agent.tools import ToolBox  # noqa: E402
from investigator import prom  # noqa: E402
from investigator.capabilities import Capabilities  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.config import Settings  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.kube import unreachable  # noqa: E402

NOW = fc.NOW


def toolbox(world, window_start):
    _, _, report = investigate(world)
    caps = Capabilities(KubernetesAdapter(fc.FakeKube(world), "shop"), EvidenceStore())
    return ToolBox(caps, Settings(), report, window_start, clock=lambda: NOW, detected_at=NOW)


# --------------------------------------------------------------------------- timing in tool results

def test_restarts_say_when_and_whether_they_are_inside_the_incident_window():
    old = toolbox(fc.world_crash(), window_start=NOW - 10)              # the crashes ended 30-35 s before detection
    res, rec = old.call("get_resource_state", {"component": "backend"})
    assert "last process end 30 s before detection (before the incident window)" in rec["summary"]
    assert all(t["in_incident_window"] is False for t in res["recent_terminations"])
    inside = toolbox(fc.world_crash(), window_start=NOW - 300)
    _, rec = inside.call("get_resource_state", {"component": "backend"})
    assert "last process end 30 s before detection" in rec["summary"] and "before the incident window" not in rec["summary"]


def test_the_agents_brief_carries_the_measured_timing_facts():
    inc = {"id": "INC-T", "signals": [{"text": "HTTP 5xx ratio at frontend is 5%"}],
           "facts": ["Incident window: 22:37:09 to detection at 22:39:34.", "Now (22:39:58) the system is healthy."]}
    assert timing(inc).startswith("Timing (measured by the platform, not inferred):\n- Incident window")
    assert timing({"id": "x"}) == ""
    tb = toolbox(fc.base_world(), NOW - 300)
    brief = InvestigatorAgent(None, tb, lambda *_: None)._messages(inc)[1]["content"]
    assert "Now (22:39:58) the system is healthy." in brief


def test_the_engine_reports_whether_the_symptoms_have_cleared(tmp_path, monkeypatch):
    eng, _, _ = engine(tmp_path, monkeypatch)
    inc = {"id": "INC-T", "detected_at": NOW, "last_unhealthy_at": NOW - 5}
    eng.system = {"t": NOW + 20, "unhealthy": False, "signals": []}
    facts, recovered = eng._timing_facts(inc)
    assert recovered and "healthy" in facts[1] and "last seen" in facts[1] and "Incident window" in facts[0]
    eng.system = {"t": NOW + 20, "unhealthy": True, "signals": ["Synthetic requests failing: 3/3"]}
    facts, recovered = eng._timing_facts(inc)
    assert not recovered and "still present: Synthetic requests failing" in facts[1]


# --------------------------------------------------------------------------- reconnecting after a cluster restart

def test_only_connection_failures_count_as_unreachable():
    assert unreachable(MaxRetryError(None, "/api", "Connection refused"))
    assert unreachable(ConnectionError("refused"))
    assert not unreachable(ValueError("bad request"))


def test_the_log_reader_reloads_its_connection_once_per_interval(monkeypatch):
    reloads, published = [], []
    tail = live.LogTailer.__new__(live.LogTailer)
    tail.kube = SimpleNamespace(reload=lambda: reloads.append(1))
    tail.bus = SimpleNamespace(publish=lambda t, d, keep=True: published.append(d["lines"][0]["text"]))
    tail.reloaded_at = 0.0
    tail.failed(MaxRetryError(None, "/api", "refused"))
    tail.failed(MaxRetryError(None, "/api", "refused"))               # within the interval: no second reload
    tail.failed(ValueError("parse"))                                   # not a connection problem: no reload
    assert len(reloads) == 1 and "read again from the kubeconfig" in published[0] and len(published) == 3


def test_the_engine_reconnects_when_the_cluster_is_unreachable(tmp_path, monkeypatch):
    eng, _, _ = engine(tmp_path, monkeypatch)
    calls = []
    eng.providers = SimpleNamespace(reconnect=lambda: calls.append(1))
    eng._reconnect(ValueError("not a connection problem"))
    eng._reconnect(MaxRetryError(None, "/api", "refused"))
    eng._reconnect(MaxRetryError(None, "/api", "refused"))
    assert calls == [1]


class Outage:
    """A detector stand-in: unreachable while `down`, then a sample in which every container 'restarted'."""

    def __init__(self):
        self.down, self.fresh = True, 0

    def sample(self):
        if self.down:
            raise MaxRetryError(None, "/api", "refused")
        return {"t": time.time(), "states": {}, "probe": {"ok": 3, "total": 3, "last": None}, "errors": {},
                "unhealthy": True, "signals": [{"kind": "process_restart", "subject": "component/backend",
                                               "t": time.time(), "text": "Container backend restarted (reason=Completed)"}]}


def test_a_restarted_cluster_does_not_open_an_incident(tmp_path, monkeypatch):
    eng, _, _ = engine(tmp_path, monkeypatch)
    eng.phase, eng.incident = "monitoring", None
    outage = Outage()
    fresh = []

    def new_detector():
        fresh.append(1)
        return outage
    eng.providers = SimpleNamespace(reconnect=lambda: None, metrics=None)
    monkeypatch.setattr(eng, "_new_detector", new_detector)
    det = eng._monitor_once(outage)                                    # unreachable: reconnect, remember the loss
    assert eng._lost_cluster and fresh == []
    outage.down = False
    det = eng._monitor_once(det)                                       # back: a fresh detector, detection paused
    assert not eng._lost_cluster and fresh == [1] and eng.cooldown_until > time.time() + 60
    eng._monitor_once(det)                                             # the restarts within the settle time
    assert eng.phase == "monitoring" and eng.incident is None


class FlakySession:
    """Fails until the connection is restored."""

    def __init__(self):
        self.up = False

    def get(self, url, params=None, timeout=None):
        if not self.up:
            raise requests.ConnectionError("port-forward is gone")
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                               json=lambda: {"status": "success", "data": {"resultType": "vector", "result": []}})


def test_prometheus_restores_its_port_forward_and_retries_once():
    restored = []

    def restore():                                                      # the port-forward comes back
        restored.append(1)
        p.http.up = True
        return True
    p = prom.Prometheus("http://127.0.0.1:9090", restore=restore)
    p.http = FlakySession()
    assert p.query("up") == [] and restored == [1]
    p.http.up, p._last_restore = False, time.time()                    # down again within the interval
    try:
        p.query("up")
        raise AssertionError("expected PrometheusError")
    except prom.PrometheusError:
        pass
    assert restored == [1]
