"""Metrics correlation (Iteration 4): metrics are time-anchored evidence, and "traffic caused the OOM" is only
said when order, component and call path support it - never because a spike merely exists."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, FakeKube, world_oom  # noqa: E402
from test_history import world_oom_after_restarts  # noqa: E402
from test_scenarios import USER_SYMPTOM  # noqa: E402

from investigator.capabilities import Capabilities, TimeRange  # noqa: E402
from investigator.capabilities.base import MetricSeries, MetricsProvider  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.context import IncidentContext  # noqa: E402
from investigator.diagnosis import correlate_traffic, diagnose  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.planner import plan_and_collect  # noqa: E402
from investigator.report import build, render_text  # noqa: E402

LIMIT = 192 * 2**20
WINDOW = (NOW - 330, NOW)


def series(fn, start=NOW - 930, end=NOW, step=5):
    return [(t, fn(t)) for t in range(int(start), int(end), step)]


class FakeMetrics(MetricsProvider):
    name = "fake-metrics"

    def __init__(self, rps=None, errors=None, memory=None, samples=None, window_s=None):
        self.rps, self.errors, self.memory, self.samples = rps, errors, memory or {}, samples or {}
        self.window_s = window_s

    def get_metrics(self, metric, time_range: TimeRange, target=None, component=None):
        clip = lambda pts: [(t, v) for t, v in pts if time_range.start <= t <= time_range.end]  # noqa: E731
        if metric == "request_rate" and self.rps:
            return [MetricSeries({"query": "rps", "window_s": self.window_s}, clip(self.rps))]
        if metric == "error_ratio" and self.errors:
            return [MetricSeries({"query": "errors"}, clip(self.errors))]
        if metric == "memory_working_set":
            return [MetricSeries({"query": "mem", "instance": i, "component": component,
                                  "samples": self.samples.get(i)}, clip(pts))
                    for i, pts in self.memory.items() if i.startswith(f"{component}-")]
        return []


def run(world, metrics):
    store = EvidenceStore(clock=lambda: NOW)
    caps = Capabilities(KubernetesAdapter(FakeKube(world), "shop"), store, metrics)
    incident = {"id": "INC-TEST", "detected_at": NOW - 30, "namespace": "shop",
                "signals": [{**s, "t": NOW - 30} for s in USER_SYMPTOM]}
    plan_and_collect(caps, IncidentContext.from_incident(incident, *WINDOW, entry=("frontend", "8080", "/api/orders"),
                                                         metrics_target="frontend"), log=lambda *_: None)
    dx = diagnose(store)
    report = build(incident, store, dx, WINDOW, True)
    render_text(report)
    return dx, store, report


def spike_at(t0):
    return series(lambda t: 100.0 if t < t0 else 800.0)


def climbing_memory(t_start, t_high):
    """Flat at 40% of the limit, then climbing linearly to 95% at t_high + 20."""
    def f(t):
        if t < t_start:
            return 0.4 * LIMIT
        return min(0.95, 0.4 + 0.55 * (t - t_start) / (t_high + 20 - t_start)) * LIMIT
    return series(f)


# The world_oom kills: backend-11111 at NOW-60, backend-22222 at NOW-50; memory warning logged at NOW-65.

def test_traffic_memory_kill_in_order_is_a_linked_contributing_factor():
    m = FakeMetrics(rps=spike_at(NOW - 200), memory={"backend-11111": climbing_memory(NOW - 190, NOW - 120)})
    dx, store, report = run(world_oom(), m)
    assert dx["category"] == "memory_exhaustion"
    [corr] = [c for c in dx["correlations"] if c["kind"] == "traffic_memory"]
    assert corr["linked"] is True
    assert dx["contributing_factors"] and "rose 8.0x" in dx["contributing_factors"][0]["statement"]
    assert "increase in incoming traffic" in dx["root_cause"]
    assert "CORRELATIONS" in render_text(report) and "[linked]" in render_text(report)


def test_traffic_that_rose_after_the_kill_did_not_start_it():
    m = FakeMetrics(rps=spike_at(NOW - 40), memory={"backend-11111": climbing_memory(NOW - 190, NOW - 120)})
    dx, _, _ = run(world_oom(), m)
    [corr] = [c for c in dx["correlations"] if c["kind"] == "traffic_memory"]
    assert corr["linked"] is False and "after the first kill" in corr["statement"]
    assert dx["contributing_factors"] == [] and "traffic" not in dx["root_cause"]


def test_a_rate_that_crosses_just_after_a_kill_may_have_risen_before_it():
    """Found in the live replay: the measured rate rose 5-10 s after the first kill, but a 15 s rate lags the real
    change by up to 15 s - so which came first is unknown, not "traffic came after"."""
    m = FakeMetrics(rps=spike_at(NOW - 55), window_s=15,
                    memory={"backend-11111": climbing_memory(NOW - 190, NOW - 120)})
    dx, store, _ = run(world_oom(), m)
    traffic = store.find(kind="metric_traffic_change")[0]
    assert traffic.t_earliest <= NOW - 60 - 10 and traffic.t_latest >= NOW - 55
    [corr] = [c for c in dx["correlations"] if c["kind"] == "traffic_memory"]
    assert corr["linked"] is None and "cannot be told" in corr["statement"]


def test_order_alone_is_not_a_link():
    """The live situation: traffic rose before the kills, but nothing measured memory on the way up."""
    flat = series(lambda t: 0.45 * LIMIT)
    m = FakeMetrics(rps=spike_at(NOW - 200), memory={"backend-11111": flat, "backend-22222": flat},
                    samples={"backend-11111": 20, "backend-22222": 22})
    dx, store, _ = run(world_oom_after_restarts(), m)
    [corr] = [c for c in dx["correlations"] if c["kind"] == "traffic_memory"]
    assert corr["linked"] is None and "only the order is known" in corr["statement"]
    assert dx["contributing_factors"] == []
    [sampling] = [c for c in dx["correlations"] if c["kind"] == "memory_sampling"]
    assert "neither confirm nor contradict" in sampling["statement"] and "45%" in sampling["statement"]
    observed = store.find(kind="metric_memory_observed")[0]
    assert observed.data["sparse"] and observed.data["spacing_s"] > 15
    assert dx["category"] == "memory_exhaustion"            # metrics that missed the peak do not argue against it


def test_traffic_at_a_component_that_does_not_call_the_killed_one_is_not_linked():
    store = EvidenceStore()
    traffic = store.add("m", "service/admin", "metric_traffic_change", "x", t=NOW - 200,
                        baseline=10.0, peak=80.0, component="admin")
    kill = store.add("k", "component/backend", "process_terminated", "y", t=NOW - 60, cause="memory_limit")

    class V:
        name = "backend"
    corr = correlate_traffic(V, traffic, [], [kill], {"frontend": {"backend"}, "admin": set()})
    assert corr["linked"] is False and "does not call" in corr["statement"]
    corr = correlate_traffic(V, store.add("m", "service/frontend", "metric_traffic_change", "x", t=NOW - 200,
                                          baseline=10.0, peak=80.0, component="frontend"), [], [kill],
                             {"frontend": {"backend"}})
    assert corr["linked"] is None                            # reaches it, precedes it, but no memory evidence


def test_an_unexamined_entry_point_makes_the_call_path_unknown_not_absent():
    """Found in the live replay: frontend was never examined, and its missing configuration was read as
    "frontend does not call backend"."""
    store = EvidenceStore()
    traffic = store.add("m", "service/frontend", "metric_traffic_change", "x", t=NOW - 200, baseline=10.0, peak=80.0,
                        component="frontend")
    kill = store.add("k", "component/backend", "process_terminated", "y", t=NOW - 60, cause="memory_limit")

    class V:
        name = "backend"
    corr = correlate_traffic(V, traffic, [], [kill], edges={}, examined={"backend"})
    assert corr["linked"] is None and "was not examined" in corr["statement"]


def test_backend_signal_alone_still_examines_the_entry_point_for_the_call_path():
    m = FakeMetrics(rps=spike_at(NOW - 200), memory={"backend-11111": climbing_memory(NOW - 190, NOW - 120)})
    store = EvidenceStore(clock=lambda: NOW)
    caps = Capabilities(KubernetesAdapter(FakeKube(world_oom()), "shop"), store, m)
    incident = {"id": "INC-TEST", "detected_at": NOW - 30, "namespace": "shop",
                "signals": [{"kind": "process_restart", "subject": "component/backend", "t": NOW - 30, "text": "restart"}]}
    plan_and_collect(caps, IncidentContext.from_incident(incident, *WINDOW, entry=None, metrics_target="frontend"),
                     log=lambda *_: None)
    decisions = [s["detail"] for s in store.trace if s["step"] == "decision"]
    assert "examine frontend" in decisions
    dx = diagnose(store)
    assert [c["linked"] for c in dx["correlations"] if c["kind"] == "traffic_memory"] == [True]


def test_an_earlier_spike_in_the_lookback_is_not_taken_for_this_incident():
    """Found in the live replay: the baseline lookback reached an earlier OOM run's spike."""
    rps = series(lambda t: 800.0 if NOW - 800 <= t < NOW - 700 or t >= NOW - 200 else 100.0)
    m = FakeMetrics(rps=rps, memory={"backend-11111": climbing_memory(NOW - 190, NOW - 120)})
    _, store, _ = run(world_oom(), m)
    traffic = store.find(kind="metric_traffic_change")[0]
    assert NOW - 210 <= traffic.t_earliest < traffic.t_latest <= NOW - 195
    assert traffic.data["earlier_episodes"] == 1 and "earlier episode" in traffic.text


def test_memory_of_instances_that_no_longer_exist_is_used_and_crossings_are_bounded():
    m = FakeMetrics(memory={"backend-11111": series(lambda t: 0.4 * LIMIT),
                            "backend-gone1": climbing_memory(NOW - 400, NOW - 300)})
    dx, store, _ = run(world_oom(), m)
    high = store.find(kind="metric_memory_high")
    assert [h.data["instance"] for h in high] == ["backend-gone1"] and high[0].data["instance_gone"]
    h = high[0]
    assert h.t_basis == "bounded" and h.t_latest - h.t_earliest == 5 and h.t_earliest < h.t_latest
    observed = store.find(kind="metric_memory_observed")[0]
    assert observed.data["instances"] == 2 and observed.data["instances_gone"] == 1 and observed.data["crossed_high"]


def test_error_ratio_onset_and_recovery_are_bounded():
    errors = series(lambda t: 0.0 if t < NOW - 150 or t >= NOW - 40 else 0.6)
    m = FakeMetrics(rps=series(lambda t: 100.0), errors=errors)
    _, store, _ = run(world_oom(), m)
    up = store.find(kind="metric_error_ratio")[0]
    down = store.find(kind="metric_error_ratio_recovered")[0]
    assert up.t_basis == down.t_basis == "bounded"
    assert up.t_earliest < NOW - 150 <= up.t_latest and down.t_earliest < NOW - 40 <= down.t_latest
    assert store.find(kind="metric_traffic_steady")            # no spike: steady traffic is stated, not a factor


def test_a_past_window_ignores_terminations_that_happened_after_it():
    """Found in the live replay: investigating yesterday's window, today's restart counted as a crash."""
    store = EvidenceStore(clock=lambda: NOW)
    caps = Capabilities(KubernetesAdapter(FakeKube(world_oom()), "shop"), store)
    incident = {"id": "INC-PAST", "detected_at": NOW - 300, "namespace": "shop",
                "signals": [{"kind": "process_restart", "subject": "component/backend", "t": NOW - 300, "text": "r"}]}
    plan_and_collect(caps, IncidentContext.from_incident(incident, NOW - 600, NOW - 200), log=lambda *_: None)
    assert not store.find(kind="process_terminated")          # the kills at NOW-60/-50 are after this window


def test_without_a_metrics_provider_nothing_is_claimed():
    dx, store, _ = run(world_oom(), None)
    assert dx["correlations"] == [] and dx["contributing_factors"] == []
    assert not [f for f in store.facts if f.kind.startswith("metric_")]
    assert any(s["detail"] == "skip metrics" and "no metrics provider" in s.get("reason", "")
               for s in store.trace if s["step"] == "decision")
