"""Read evidence saved before Iteration 3 step 4 (Kubernetes-worded facts) with today's engine.

Old files used fact kinds such as `container_terminated` / `k8s_event` and keys such as `pod`/`workload`,
and carried no neutral `cause`/`category`. This shim rewrites them into the current vocabulary, reusing
the Kubernetes adapter's own mappings (all those old files came from Kubernetes). Only `replay` uses it.
"""
from .capabilities.kubernetes import WAITING_CAUSES, event_category, termination_cause

KINDS = {"workload_status": "component_status", "pod_status": "instance_status", "container_waiting": "process_waiting",
         "container_terminated": "process_terminated", "k8s_event": "event", "node_event": "infrastructure_event",
         "backing_workload": "backing_component", "rollout": "change"}
KEYS = {"workload": "component", "pod": "instance", "container": "process", "from_pod": "from_instance"}


def is_legacy(store: dict) -> bool:
    return any(f["kind"] in KINDS or str(f.get("subject", "")).startswith("workload/") for f in store.get("facts", []))


def upgrade(store: dict) -> dict:
    facts = []
    for f in store.get("facts", []):
        f = {**f, "data": dict(f.get("data") or {})}
        d = f["data"]
        if f["kind"] == "log_signature":          # old: pods = instances, instances = generations
            d["generations"] = d.pop("instances", [])
            d["instances"] = d.pop("pods", [])
        if f["kind"] == "log_exception":           # old: instance = generation
            d["generation"] = d.pop("instance", None)
        if f["kind"] == "workload_status":
            d["instances"] = d.pop("pods", [])
        for old, new in KEYS.items():
            if old in d and new not in d:
                d[new] = d.pop(old)
        f["kind"] = KINDS.get(f["kind"], f["kind"])
        if str(f.get("subject", "")).startswith("workload/"):
            f["subject"] = "component/" + f["subject"][len("workload/"):]
        if f["kind"] == "process_terminated":
            d.setdefault("cause", termination_cause(d.get("reason"), d.get("exit_code")))
        if f["kind"] == "process_waiting":
            d.setdefault("cause", WAITING_CAUSES.get(d.get("reason")))
        if f["kind"] in ("event", "infrastructure_event"):
            d.setdefault("category", event_category(d.get("reason"), d.get("message", "")))
        if f["kind"] == "change":
            d.setdefault("change", "rollout")
        facts.append(f)
    return {**store, "facts": facts}
