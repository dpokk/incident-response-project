"""Kubernetes evidence recorder (Iteration 4): what it retains, and what it refuses to record wrongly."""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, FakeKube, _j, base_world  # noqa: E402

from investigator.capabilities.kubernetes_recorder import KubernetesRecorder  # noqa: E402
from investigator.history_store import HistoryStore  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_recorder(tmp_path, max_lines=3000):
    clock = Clock(NOW - 600)
    kube = FakeKube(base_world())
    store = HistoryStore(tmp_path / "history.db", clock=clock, max_lines_per_minute=max_lines)
    rec = KubernetesRecorder(kube, "shop", store, clock=clock, log=lambda *_: None)
    rec.start(threads=False)
    return rec, kube, store, clock


def backend(kube):
    return copy.deepcopy(next(p for p in kube.w["pods"] if p["name"] == "backend-11111"))


def oom_killed(pod, restarts, finished):
    c = pod["containers"][0]
    c["restart_count"] = restarts
    c["last_state"] = {"state": "terminated", "reason": "OOMKilled", "exit_code": 137, "started_at": finished - 30,
                       "finished_at": finished}
    c["state"] = {"state": "running", "started_at": finished + 1}
    return pod


def restart_in_cluster(kube, pod):
    kube.w["pods"] = [pod if p["name"] == pod["name"] else p for p in kube.w["pods"]]


def test_each_termination_is_kept_with_the_run_it_ended_and_its_logs(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    kube.w["logs"][("backend-11111", True)] = [(NOW - 320, _j(level="warning", msg="memory usage approaching container limit"))]
    clock.t = NOW - 300
    restart_in_cluster(kube, oom_killed(backend(kube), 1, NOW - 301))
    rec.poll_once()                         # the poll sees the restart and fetches the ended run's logs at once
    terms = store.lifecycle("shop", "backend", NOW - 900, NOW, "terminated")
    assert [(t["instance"], t["data"]["generation"], t["data"]["reason"], t["t_basis"]) for t in terms] == \
        [("backend-11111", 0, "OOMKilled", "exact")]
    # The ended run's logs were fetched while Kubernetes still had them, labelled as that run.
    assert store.log_lines("shop", "backend-11111", "backend", 0, NOW - 900, NOW) == \
        kube.w["logs"][("backend-11111", True)]


def test_previous_logs_are_not_stored_under_the_wrong_run(tmp_path):
    """If the container restarted again before the fetch, "previous" is a later run: store nothing."""
    rec, kube, store, clock = make_recorder(tmp_path)
    rec.pods["backend-11111"] = oom_killed(backend(kube), 3, NOW - 100)
    kube.w["logs"][("backend-11111", True)] = [(NOW - 101, "a line of run 3")]
    assert rec.backfill_previous("backend-11111", "backend", "backend", generation=0) is None
    assert store.log_generations("shop", "backend", NOW - 900, NOW) == []


def test_an_empty_previous_log_is_retried_on_later_polls(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    restart_in_cluster(kube, oom_killed(backend(kube), 1, NOW - 301))
    rec.poll_once()                                          # Kubernetes has nothing for the ended run yet
    assert store.log_generations("shop", "backend", NOW - 900, NOW) == []
    kube.w["logs"][("backend-11111", True)] = [(NOW - 302, "last words")]
    rec.poll_once()
    assert store.log_lines("shop", "backend-11111", "backend", 0, NOW - 900, NOW) == [(NOW - 302, "last words")]


def test_current_run_lines_are_captured_once_and_only_from_that_run(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    pod = backend(kube)                          # current run started at NOW - 3000 (generation 0)
    kube.w["logs"][("backend-11111", False)] = [(NOW - 3100, "a line from before this run started"),
                                                (NOW - 50, _j(level="info", msg="stats")), (NOW - 49, "plain line")]
    assert rec.capture_current(pod) == 2
    assert rec.capture_current(pod) == 0         # the next poll overlaps: nothing is stored twice
    [g] = store.log_generations("shop", "backend", NOW - 4000, NOW)
    assert (g["instance"], g["generation"], g["lines"]) == ("backend-11111", 0, 2)


def test_deleted_pods_are_remembered(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)          # first poll at NOW - 600
    kube.w["pods"] = [p for p in kube.w["pods"] if p["name"] != "backend-11111"]
    clock.t = NOW - 595
    rec.poll_once()                                          # the pod is missing from this snapshot
    inst = next(i for i in store.instances("shop", "backend", NOW - 900, NOW) if i["instance"] == "backend-11111")
    assert inst["gone_at"] == NOW - 595 and inst["component"] == "backend"
    assert store.component_of("shop", "backend-11111") == "backend"
    [gone] = store.lifecycle("shop", "backend", NOW - 900, NOW, "deleted")
    assert gone["t_basis"] == "observed" and gone["data"]["not_before"] == NOW - 600   # between two polls


def test_a_restart_then_deletion_between_polls_still_records_the_termination(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    restart_in_cluster(kube, oom_killed(backend(kube), 1, NOW - 590))
    clock.t = NOW - 595
    rec.poll_once()
    kube.w["pods"] = [p for p in kube.w["pods"] if p["name"] != "backend-11111"]
    clock.t = NOW - 590
    rec.poll_once()
    assert [t["data"]["reason"] for t in store.lifecycle("shop", "backend", NOW - 900, NOW, "terminated")] == ["OOMKilled"]
    assert store.lifecycle("shop", "backend", NOW - 900, NOW, "deleted")


def test_events_configuration_and_definitions_are_versioned_and_secrets_never_stored(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    kube.w["events"] = [{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container",
                         "object_kind": "Pod", "object_name": "backend-11111", "count": 3, "first": NOW - 500,
                         "last": NOW - 450}]
    clock.t = NOW - 400
    rec.poll_once()
    kube.w["configmaps"][0]["data"]["DATABASE_URL"] = "postgresql://shop@postgres-wrong:5432/shop"
    kube.w["secrets"]["postgres-credentials"]["POSTGRES_PASSWORD"] = "rotated"
    clock.t = NOW - 390
    rec.poll_once()
    ev = store.events("shop", NOW - 900, NOW)
    assert [(e["component"], e["reason"], e["first"]) for e in ev] == [("backend", "BackOff", NOW - 500)]
    cm = store.versions("shop", "ConfigMap", "backend-config", NOW - 900, NOW)
    assert [v["content"]["DATABASE_URL"].split("@")[1] for v in cm] == ["postgres:5432/shop", "postgres-wrong:5432/shop"]
    assert cm[1]["previous_checked_at"] == NOW - 400
    assert len(store.versions("shop", "Secret", "postgres-credentials", NOW - 900, NOW)) == 2
    assert len(store.versions("shop", "Deployment", "backend", NOW - 900, NOW)) == 1      # unchanged: one version
    raw = (tmp_path / "history.db").read_bytes() + b"".join(
        p.read_bytes() for p in tmp_path.glob("history.db-*"))
    assert b"s3cret" not in raw and b"rotated" not in raw


def test_availability_is_recorded_only_when_it_changes(tmp_path):
    from investigator.capabilities.kubernetes_recorder import ENDPOINTS_KIND, STATUS_KIND
    rec, kube, store, clock = make_recorder(tmp_path)              # first poll at NOW - 600: postgres 1 ready
    clock.t = NOW - 595
    rec.poll_once()                                                # unchanged: no new version
    kube.w["endpoints"]["postgres"] = 0
    kube.w["workloads"][2].update(replicas_desired=0, replicas_ready=0)
    clock.t = NOW - 590
    rec.poll_once()
    eps = store.versions("shop", ENDPOINTS_KIND, "postgres", NOW - 900, NOW)
    assert [v["content"]["ready"] for v in eps] == [1, 0]
    assert (eps[1]["previous_checked_at"], eps[1]["observed_at"]) == (NOW - 595, NOW - 590)   # bounded change
    assert [v["content"] for v in store.versions("shop", STATUS_KIND, "postgres", NOW - 900, NOW)] == \
        [{"desired": 1, "ready": 1}, {"desired": 0, "ready": 0}]


def test_recording_sessions_define_coverage(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    clock.t = NOW - 100
    rec.poll_once()
    [s] = store.sessions("shop", NOW - 900, NOW)
    assert (s["started_at"], s["last_seen_at"]) == (NOW - 600, NOW - 100)


def test_line_cap_counts_what_it_drops(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path, max_lines=3)
    minute = (NOW // 60) * 60
    stored = store.add_log_lines("shop", "frontend", "frontend-aaaaa", "frontend", 0,
                                 [(minute + i, f"line {i}") for i in range(7)])
    assert stored == 3
    assert [g["dropped"] for g in store.log_gaps("shop", "frontend-aaaaa", "frontend", 0, minute, minute + 60)] == [4]


def test_retention_prunes_old_evidence_but_keeps_the_latest_definition(tmp_path):
    rec, kube, store, clock = make_recorder(tmp_path)
    store.add_log_lines("shop", "backend", "backend-11111", "backend", 0, [(NOW - 590, "old line")])
    clock.t = NOW + 7200
    store.prune(NOW)
    assert store.stats()["log_lines"] == 0
    assert store.versions("shop", "ConfigMap", "backend-config", NOW - 900, NOW + 7200)    # baseline kept
