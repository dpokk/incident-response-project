"""Detection tests (Iteration 3 step 3): the detector works through capabilities only and reports symptoms."""
import copy
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, FakeKube, base_world, world_crash, world_db_down  # noqa: E402

from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.detector import Detector  # noqa: E402

SETTINGS = SimpleNamespace(poll_interval_s=5, entry_service="frontend", entry_port="8080", entry_path="/api/orders",
                           probe_failure_threshold=0.5, error_log_threshold=5, error_rate_threshold=0.05,
                           not_ready_grace_s=20, entry_app="frontend")


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
    assert ("container_restart", "workload/backend") in kinds(det.sample())


def test_crash_loop_waiting_is_reported_immediately():
    det, _ = detector(world_crash())
    assert ("container_waiting", "workload/backend") in kinds(det.sample())


def test_failing_user_requests_need_two_polls():
    det, _ = detector(world_db_down())
    first, second = det.sample(), det.sample()
    assert ("entry_probe_failure", "workload/frontend") not in kinds(first)
    assert ("entry_probe_failure", "workload/frontend") in kinds(second)


def test_signals_describe_symptoms_not_causes():
    det, _ = detector(world_db_down())
    det.sample()
    for s in det.sample()["signals"]:
        assert not any(w in s["text"].lower() for w in ("misconfig", "unavailable dependency", "root cause")), s
