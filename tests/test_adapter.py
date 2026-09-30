"""Kubernetes adapter behaviour that the fake-cluster scenario tests don't exercise."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fake_cluster import NOW, FakeKube, base_world  # noqa: E402

from investigator.capabilities import Capabilities, TimeRange  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.collect import record_resource_state  # noqa: E402
from investigator.diagnosis import diagnose  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402


def journal(tmp_path, *entries):
    p = tmp_path / "pod_journal.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


def crashed(pod, app="backend", t=NOW - 100):
    return {"namespace": "shop", "pod": pod, "app": app, "t": t, "kind": "container_terminated", "container": "backend",
            "reason": "Error", "exit_code": 1, "started_at": t - 20, "restart_count": 1}


def test_terminations_of_deleted_instances_are_kept_as_evidence(tmp_path):
    """Found in the final live run: a pod crashed, then a rollout replaced it before the investigation ran."""
    path = journal(tmp_path, crashed("backend-oldrs-zzzzz"), crashed("frontend-oldrs-yyyyy", app="frontend"))
    adapter = KubernetesAdapter(FakeKube(base_world()), "shop", journal_path=path)
    rs = adapter.get_resource_state("backend", TimeRange(NOW - 300, NOW))
    assert [(h.instance, h.instance_gone) for h in rs.history] == [("backend-oldrs-zzzzz", True)]

    store = EvidenceStore()
    record_resource_state(store, Capabilities(adapter, store), rs, NOW - 300)
    term = next(f for f in store.facts if f.kind == "process_terminated")
    assert term.data["instance_gone"] and "no longer exists" in term.text

    dx = diagnose(store)
    assert dx["category"] == "application_crash"
    assert "could not be determined" in dx["root_cause"]
    assert dx["confidence_label"] == "Low"          # one exit, no logs: honest, low-confidence


def test_current_instances_still_reported_normally(tmp_path):
    path = journal(tmp_path, crashed("backend-11111"))
    rs = KubernetesAdapter(FakeKube(base_world()), "shop", journal_path=path).get_resource_state(
        "backend", TimeRange(NOW - 300, NOW))
    assert [(h.instance, h.instance_gone) for h in rs.history] == [("backend-11111", False)]
