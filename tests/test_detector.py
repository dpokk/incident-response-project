"""Detection tests (Iteration 3 step 3): the detector works through capabilities only and reports symptoms."""
import copy
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, FakeKube, base_world, world_crash, world_db_down  # noqa: E402

from investigator.capabilities.base import MetricSeries  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.detector import Detector  # noqa: E402

SETTINGS = SimpleNamespace(poll_interval_s=5, entry_service="frontend", entry_port="8080", entry_path="/api/orders",
                           probe_failure_threshold=0.5, error_log_threshold=5, error_rate_threshold=0.05,
                           not_ready_grace_s=20, entry_app="frontend", lagging_sustain_s=30)


def detector(world):
    kube = FakeKube(world)
    return Detector(SETTINGS, KubernetesAdapter(kube, "shop"), None, clock=lambda: NOW), kube


def kinds(sample):
    return {(s["kind"], s["subject"]) for s in sample["signals"]}


def test_healthy_cluster_raises_nothing():
    det, _ = detector(base_world())
    det.sample()
    assert det.sample()["signals"] == []


def test_restart_is_detected_between_samples():
    world = base_world()
    det, kube = detector(world)
    det.sample()
    crashed = copy.deepcopy(world_crash()["pods"][1])
    kube.w["pods"][1] = crashed
    assert ("process_restart", "component/backend") in kinds(det.sample())


def test_crash_loop_waiting_is_reported_immediately():
    det, _ = detector(world_crash())
    assert ("process_waiting", "component/backend") in kinds(det.sample())


def test_failing_user_requests_need_two_polls():
    det, _ = detector(world_db_down())
    first, second = det.sample(), det.sample()
    assert ("entry_probe_failure", "component/frontend") not in kinds(first)
    assert ("entry_probe_failure", "component/frontend") in kinds(second)


def test_signals_describe_symptoms_not_causes():
    det, _ = detector(world_db_down())
    det.sample()
    for s in det.sample()["signals"]:
        assert not any(w in s["text"].lower() for w in ("misconfig", "unavailable dependency", "root cause")), s


# --------------------------------------------------------------------------- short blips (lagging signals)

class Scripted:
    """The fake cluster's healthy state, with a scripted probe, frontend error log lines and a 5xx ratio."""
    name = "fake-metrics"

    def __init__(self, world):
        self.inner, self.probe_ok, self.lines, self.ratio = KubernetesAdapter(FakeKube(world), "shop"), True, [], 0.0

    def __getattr__(self, k):
        return getattr(self.inner, k)

    def probe_request(self, *_):
        return SimpleNamespace(status=200 if self.probe_ok else 503, body="ok" if self.probe_ok else '{"error": "x"}')

    def get_logs(self, component, instance, process, tr, previous=False):
        if component != "frontend":
            return []
        return [(t, '{"level": "error", "msg": "upstream request to backend failed", "count": %d}' % n)
                for t, n in self.lines if tr.start <= t <= tr.end]

    def get_metrics(self, metric, tr, target=None, component=None):
        return [MetricSeries({}, [(tr.end, self.ratio)])]


def polls(world=None, step=SETTINGS.poll_interval_s):
    """A detector on the scripted cluster, and a function that advances the clock by one poll and samples."""
    clock = {"t": NOW}
    cl = Scripted(world or base_world())
    det = Detector(SETTINGS, cl, cl, clock=lambda: clock["t"])

    def poll():
        clock["t"] += step
        return det.sample()
    poll()
    return cl, clock, poll


def test_a_log_line_is_counted_in_one_poll_only():
    cl, clock, poll = polls()
    cl.lines = [(clock["t"] + 4.5, 92)]           # inside both this poll's and the next poll's fetch window
    assert poll()["errors"]["frontend"] == 92
    assert poll()["errors"]["frontend"] == 0       # fetched again, not counted again


def test_a_short_stall_does_not_open_an_incident():
    cl, clock, poll = polls()
    cl.lines = [(clock["t"] + 3, 92)]              # one burst of upstream timeouts from a 3-second stall
    cl.ratio = 0.05                                # stays inside the 15 s rate window for several polls
    seen = [kinds(poll()) for _ in range(4)]
    assert not any(seen), seen                     # the probe kept passing: no incident


def test_a_lagging_signal_on_its_own_opens_an_incident_once_it_has_held():
    cl, clock, poll = polls()
    cl.ratio = 0.2
    seen = [("http_5xx_ratio", "component/frontend") in kinds(poll()) for _ in range(7)]
    assert seen == [False] * 6 + [True]            # 30 s at 5 s polls: the seventh poll


def test_the_sustain_time_is_measured_in_seconds_not_polls():
    cl, clock, poll = polls(step=8)                # a slow poll (probes and log reads take time)
    cl.ratio = 0.2
    seen = [("http_5xx_ratio", "component/frontend") in kinds(poll()) for _ in range(5)]
    assert seen == [False] * 4 + [True]            # held 32 s at the fifth poll


def test_with_the_probe_failing_lagging_signals_need_only_two_polls():
    cl, clock, poll = polls()
    cl.ratio, cl.probe_ok = 0.6, False
    first, second = kinds(poll()), kinds(poll())
    assert not first and {("entry_probe_failure", "component/frontend"), ("http_5xx_ratio", "component/frontend")} <= second
