"""Evidence saved before the neutral vocabulary (Iteration 2 / early Iteration 3) can still be replayed."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from investigator import legacy  # noqa: E402
from investigator.diagnosis import diagnose  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402

OLD = {"facts": [
    {"id": "F1", "source": "kubernetes.workloads", "subject": "workload/backend", "kind": "workload_status",
     "text": "Deployment shop/backend: 0/2 replicas ready", "t": None,
     "data": {"workload": "backend", "desired": 2, "ready": 0, "available": 0, "pods": ["b-1"], "limits": {}}},
    {"id": "F2", "source": "kubernetes.pod_status", "subject": "workload/backend", "kind": "pod_status",
     "text": "Pod b-1: phase=Running, ready=False, restarts=3", "t": None,
     "data": {"pod": "b-1", "phase": "Running", "ready": False, "restarts": 3, "unschedulable": False, "ready_since": None}},
    {"id": "F3", "source": "kubernetes.pod_status", "subject": "workload/backend", "kind": "container_terminated",
     "text": "Container backend in pod b-1 terminated: reason=OOMKilled, exit code 137", "t": 100.0,
     "data": {"pod": "b-1", "container": "backend", "reason": "OOMKilled", "exit_code": 137, "ran_s": 20,
              "memory_limit": 201326592, "restarts": 3}},
    {"id": "F4", "source": "kubernetes.pod_status", "subject": "workload/backend", "kind": "container_terminated",
     "text": "Container backend in pod b-1 terminated: reason=OOMKilled, exit code 137", "t": 130.0,
     "data": {"pod": "b-1", "container": "backend", "reason": "OOMKilled", "exit_code": 137, "ran_s": 5,
              "memory_limit": 201326592, "restarts": 3}},
    {"id": "F5", "source": "kubernetes.events", "subject": "workload/backend", "kind": "k8s_event",
     "text": "Warning event BackOff on Pod b-1", "t": 110.0,
     "data": {"reason": "BackOff", "type": "Warning", "count": 3, "object_kind": "Pod", "message": "Back-off", "last": 140}},
], "trace": [{"step": "tool", "detail": "get_pods", "status": "ok"}]}


def test_legacy_evidence_is_detected_and_upgraded():
    assert legacy.is_legacy(OLD)
    new = legacy.upgrade(OLD)
    assert not legacy.is_legacy(new)
    kinds = [f["kind"] for f in new["facts"]]
    assert kinds == ["component_status", "instance_status", "process_terminated", "process_terminated", "event"]
    assert new["facts"][2]["data"]["cause"] == "memory_limit" and new["facts"][2]["data"]["instance"] == "b-1"
    assert new["facts"][4]["data"]["category"] == "restart_backoff"


def test_upgraded_legacy_evidence_still_diagnoses():
    dx = diagnose(EvidenceStore.from_dict(legacy.upgrade(OLD)))
    assert dx["category"] == "memory_exhaustion" and dx["affected_component"] == "backend"
