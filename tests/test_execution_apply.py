"""Iteration 7, Milestone 2: live recheck, dry run, the single typed change, and the Kubernetes actuator mapping.
Everything runs against fakes (the fake cluster for reads, a fake actuator / fake API client for writes)."""
import copy
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fake_cluster as fc  # noqa: E402
from test_execution import EXECUTOR, Clock, make_service, req  # noqa: E402

from investigator.actuators.base import ChangeResult  # noqa: E402
from investigator.actuators.kubernetes import RESTART_ANNOTATION, KubernetesActuator  # noqa: E402
from investigator.capabilities import Capabilities  # noqa: E402
from investigator.capabilities.kubernetes import KubernetesAdapter  # noqa: E402
from investigator.evidence import EvidenceStore  # noqa: E402
from investigator.execution_model import ChangeRequest, Refusal  # noqa: E402
from investigator.recheck import recheck, replace_host  # noqa: E402

NOW = fc.NOW
MI = 2**20
SMP = "application/strategic-merge-patch+json"


def caps_for(world):
    return lambda: Capabilities(KubernetesAdapter(fc.FakeKube(world), "shop"), EvidenceStore())


def mem(expected=192, target=256):
    return ChangeRequest("adjust_resource_limit", "backend", process="backend", expected_bytes=expected * MI,
                         target_bytes=target * MI)


def scale(expected=0, target=1):
    return ChangeRequest("scale_workload", "postgres", expected_replicas=expected, target_replicas=target)


def config(expected="postgres-wrong", target="postgres"):
    return ChangeRequest("restore_configuration", "backend", source="configmap/backend-config", item="DATABASE_URL",
                         expected_host=expected, target_host=target)


def with_limit(world, value):
    w = copy.deepcopy(world)
    w["workloads"][1]["containers"][0]["limits"]["memory"] = value
    return w


# --------------------------------------------------------------------------- live recheck semantics

def test_memory_limit_recheck_expected_target_and_changed():
    ok = recheck(caps_for(fc.world_oom())(), mem(), NOW, 900)
    assert ok.ok and ok.write == {"component": "backend", "process": "backend", "expected_bytes": 192 * MI,
                                  "new_bytes": 256 * MI}
    assert {c["check"] for c in ok.checks} == {"component_exists", "current_value", "still_relevant"}
    done = recheck(caps_for(with_limit(fc.world_oom(), "256Mi"))(), mem(), NOW, 900)
    assert done.code == Refusal.NOT_NEEDED and "already applied" in done.message
    moved = recheck(caps_for(with_limit(fc.world_oom(), "384Mi"))(), mem(), NOW, 900)
    assert moved.code == Refusal.STATE_CHANGED and "384Mi" in moved.message


def test_memory_limit_recheck_requires_the_condition_to_still_hold():
    healthy = recheck(caps_for(fc.base_world())(), mem(), NOW, 900)
    assert healthy.code == Refusal.NOT_NEEDED and "no longer holds" in healthy.message
    assert recheck(caps_for(fc.base_world())(), mem(), NOW, 900, require_relevance=False).ok   # e.g. a rollback
    w = copy.deepcopy(fc.world_oom())
    for p in w["pods"]:
        p["ready"] = True
    old = recheck(caps_for(w)(), mem(), NOW + 3600, 900)       # recovered, and its kills are older than the lookback
    assert old.code == Refusal.NOT_NEEDED
    assert recheck(caps_for(w)(), mem(), NOW, 900).ok        # recovered, but killed at the limit within the lookback


def test_a_missing_component_or_unreadable_state_never_leads_to_a_change():
    w = copy.deepcopy(fc.world_oom())
    w["workloads"] = [x for x in w["workloads"] if x["name"] != "backend"]
    gone = recheck(caps_for(w)(), mem(), NOW, 900)
    assert gone.code == Refusal.STATE_CHANGED and "no longer exists" in gone.message

    class Broken(fc.FakeKube):
        def workloads(self, ns):
            raise ConnectionError("api unreachable")
    broken = recheck(Capabilities(KubernetesAdapter(Broken(fc.world_oom()), "shop"), EvidenceStore()), mem(), NOW, 900)
    assert broken.code == Refusal.RECHECK_FAILED and "nothing was changed" in broken.message


def test_replica_recheck_expected_target_and_changed():
    ok = recheck(caps_for(fc.world_db_down())(), scale(), NOW, 900)
    assert ok.ok and ok.write == {"component": "postgres", "expected": 0, "new": 1}
    w = copy.deepcopy(fc.world_db_down())
    w["workloads"][2]["replicas_desired"] = 1
    assert recheck(caps_for(w)(), scale(), NOW, 900).code == Refusal.NOT_NEEDED
    w["workloads"][2]["replicas_desired"] = 2
    assert recheck(caps_for(w)(), scale(), NOW, 900).code == Refusal.STATE_CHANGED


def test_configuration_recheck_expected_target_changed_and_target_availability():
    ok = recheck(caps_for(fc.world_db_misconfig())(), config(), NOW, 900)
    assert ok.ok and ok.write["new_value"] == "postgresql://shop@postgres:5432/shop"
    assert ok.write["expected_value"] == "postgresql://shop@postgres-wrong:5432/shop"
    assert ok.observed == {"live_host": "postgres-wrong", "target_host": "postgres", "target_exists": True,
                           "target_ready_endpoints": 1}
    assert recheck(caps_for(fc.base_world())(), config(), NOW, 900).code == Refusal.NOT_NEEDED   # already postgres
    w = copy.deepcopy(fc.world_db_misconfig())
    w["configmaps"][0]["data"]["DATABASE_URL"] = "postgresql://shop@elsewhere:5432/shop"
    assert recheck(caps_for(w)(), config(), NOW, 900).code == Refusal.STATE_CHANGED
    w = copy.deepcopy(fc.world_db_misconfig())
    w["endpoints"]["postgres"] = 0
    down = recheck(caps_for(w)(), config(), NOW, 900)
    assert down.code == Refusal.STATE_CHANGED and "not available now" in down.message


def test_only_the_host_is_replaced():
    assert replace_host("postgresql://shop@postgres-wrong:5432/shop", "postgres-wrong", "postgres") == \
        "postgresql://shop@postgres:5432/shop"
    assert replace_host("postgres-wrong", "postgres-wrong", "postgres") == "postgres"
    assert replace_host("postgresql://shop@other:5432/shop", "postgres-wrong", "postgres") is None


# --------------------------------------------------------------------------- the executor's sequence

class Actuator:
    """Fake writer: records every call; can reject the dry run, reject the write, fail part-way or raise."""
    name, scope = "fake", "shop"

    def __init__(self, dry_ok=True, apply_ok=True, partial=False, raise_on_apply=False):
        self.calls, self.dry_ok, self.apply_ok, self.partial, self.raise_on_apply = [], dry_ok, apply_ok, partial, raise_on_apply

    def _r(self, op, dry_run, **kw):
        self.calls.append((op, dry_run, kw))
        if not dry_run and self.raise_on_apply:
            raise TimeoutError("connection lost after sending")
        ok = self.dry_ok if dry_run else self.apply_ok
        return ChangeResult(ok, dry_run, op, "fake", before=kw.get("before"), after=kw.get("after"),
                            error=None if ok else "admission denied", changed=(not dry_run) and (ok or self.partial))

    def set_memory_limit(self, component, process, expected_bytes, new_bytes, dry_run):
        return self._r("set_memory_limit", dry_run, expected=expected_bytes, new=new_bytes)

    def set_replicas(self, component, expected, new, dry_run):
        return self._r("set_replicas", dry_run, expected=expected, new=new)

    def set_config_value(self, source, item, expected_value, new_value, restart_component, dry_run):
        return self._r("set_config_value", dry_run, before=expected_value, after=new_value, restart=restart_component)


def service(tmp_path, world=None, actuator=None, approve=("256Mi",), plan_world=None):
    svc, review, d, plan, _, clock = make_service(tmp_path, world=plan_world or world or fc.world_oom(),
                                                  approve=approve)
    svc.actuator = actuator or Actuator()
    svc.capabilities = caps_for(world or fc.world_oom())
    return svc, d


def test_a_valid_request_rechecks_dry_runs_then_applies_exactly_one_change(tmp_path):
    svc, d = service(tmp_path)
    out = svc.execute(req(d))
    assert out.ok and out.code == "verifying"
    assert [(op, dry) for op, dry, _ in svc.actuator.calls] == [("set_memory_limit", True), ("set_memory_limit", False)]
    assert svc.actuator.calls[1][2] == {"expected": 192 * MI, "new": 256 * MI}
    rec = svc.store.for_action("INC-TEST", d, 0)
    assert rec["status"] == "verifying" and rec["recheck"]["ok"] and rec["dry_run"]["accepted"]
    assert rec["applied"]["accepted"] and rec["applied"]["at"] and rec["executor"] == EXECUTOR
    assert [c["check"] for c in out.checks][-1] == "dry_run"


def test_a_failed_dry_run_prevents_any_mutation(tmp_path):
    svc, d = service(tmp_path, actuator=Actuator(dry_ok=False))
    out = svc.execute(req(d))
    assert out.code == Refusal.DRY_RUN_FAILED.value and "nothing was changed" in out.message
    assert [dry for _, dry, _ in svc.actuator.calls] == [True]
    rec = svc.store.for_action("INC-TEST", d, 0)
    assert rec["status"] == "refused" and rec["dry_run"]["error"] == "admission denied" and rec["applied"] is None


def test_a_changed_state_refuses_before_any_write_and_the_action_is_closed(tmp_path):
    svc, d = service(tmp_path, world=with_limit(fc.world_oom(), "384Mi"), plan_world=fc.world_oom())
    out = svc.execute(req(d))
    assert out.code == Refusal.STATE_CHANGED.value and svc.actuator.calls == []
    assert svc.execute(req(d, t=NOW + 1)).code == Refusal.ALREADY_COMPLETED.value


def test_an_already_remediated_state_is_not_applied_again(tmp_path):
    svc, d = service(tmp_path, world=with_limit(fc.world_oom(), "256Mi"), plan_world=fc.world_oom())
    out = svc.execute(req(d))
    assert out.code == Refusal.NOT_NEEDED.value and svc.actuator.calls == []


def test_a_duplicate_request_after_success_never_writes_twice(tmp_path):
    svc, d = service(tmp_path)
    svc.execute(req(d))
    for i in range(3):
        assert svc.execute(req(d, t=NOW + i)).code in (Refusal.IN_PROGRESS.value, Refusal.ALREADY_COMPLETED.value)
    assert sum(1 for _, dry, _ in svc.actuator.calls if not dry) == 1


def test_uncertain_and_failed_writes_are_recorded_and_never_retried(tmp_path):
    svc, d = service(tmp_path, actuator=Actuator(raise_on_apply=True))
    out = svc.execute(req(d))
    assert out.code == "uncertain" and "Not retried" in out.message
    assert svc.execute(req(d)).code == Refusal.UNCERTAIN.value
    svc, d = service(tmp_path / "b", actuator=Actuator(apply_ok=False, partial=True))
    assert svc.execute(req(d)).code == "uncertain"
    svc, d = service(tmp_path / "c", actuator=Actuator(apply_ok=False))
    out = svc.execute(req(d))
    assert out.code == "apply_failed" and "nothing was changed" in out.message


def test_each_action_type_reaches_its_own_typed_operation(tmp_path):
    for world, value, op in ((fc.world_db_down(), "1", "set_replicas"),
                             (fc.world_db_misconfig(), "postgres", "set_config_value")):
        svc, d = service(tmp_path / op, world=world, approve=(value,))
        assert svc.execute(req(d)).ok
        assert {o for o, _, _ in svc.actuator.calls} == {op}


def test_configuration_values_are_audited_as_hosts_only(tmp_path):
    svc, d = service(tmp_path, world=fc.world_db_misconfig(), approve=("postgres",))
    svc.execute(req(d))
    rec = svc.store.for_action("INC-TEST", d, 0)
    assert (rec["applied"]["before"], rec["applied"]["after"]) == ("postgres-wrong", "postgres")
    assert "postgresql://" not in str(rec["applied"]) + str(rec["dry_run"]) + str(rec["recheck"])


def test_no_cluster_connection_means_no_execution(tmp_path):
    svc, d = service(tmp_path)
    svc.capabilities = None
    out = svc.execute(req(d))
    assert out.code == Refusal.RECHECK_FAILED.value and svc.actuator.calls == []


# --------------------------------------------------------------------------- the Kubernetes actuator mapping

class FakeAPI:
    """Records reads and patches like the kubernetes client's AppsV1Api/CoreV1Api."""

    def __init__(self, fail_second_patch=False):
        self.patches, self.fail_second_patch = [], fail_second_patch
        self.dep = NS(metadata=NS(resource_version="41"), spec=NS(replicas=0, template=NS(spec=NS(containers=[
            NS(name="backend", resources=NS(limits={"memory": "192Mi", "cpu": "500m"}))]))))
        self.cm = NS(metadata=NS(resource_version="7"), data={"DATABASE_URL": "postgresql://shop@postgres-wrong:5432/shop"})

    def read_namespaced_deployment(self, name, ns):
        return self.dep

    def read_namespaced_config_map(self, name, ns):
        return self.cm

    def _patch(self, kind, name, ns, body, kw):
        self.patches.append((kind, name, ns, body, kw))
        if self.fail_second_patch and len(self.patches) == 2:
            raise RuntimeError("api server unavailable")
        spec = body.get("spec", {})
        limits = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("resources", {}).get("limits")
        return NS(spec=NS(replicas=spec.get("replicas", 0), template=NS(spec=NS(containers=[
            NS(name="backend", resources=NS(limits=limits or {}))]))), data=body.get("data"))

    def patch_namespaced_deployment(self, name, ns, body, **kw):
        return self._patch("Deployment", name, ns, body, kw)

    def patch_namespaced_config_map(self, name, ns, body, **kw):
        return self._patch("ConfigMap", name, ns, body, kw)


def actuator(api=None):
    api = api or FakeAPI()
    return KubernetesActuator(NS(apps=api, core=api), "shop"), api


def test_memory_limit_maps_to_one_deployment_patch_with_dry_run_and_precondition():
    a, api = actuator()
    dry = a.set_memory_limit("backend", "backend", 192 * MI, 512 * MI, dry_run=True)
    assert dry.accepted and dry.dry_run and not dry.changed and dry.after == "512Mi"
    kind, name, ns, body, kw = api.patches[0]
    assert (kind, name, ns, kw) == ("Deployment", "backend", "shop", {"dry_run": "All", "_content_type": SMP})
    assert body == {"metadata": {"resourceVersion": "41"}, "spec": {"template": {"spec": {"containers": [
        {"name": "backend", "resources": {"limits": {"memory": "512Mi"}}}]}}}}
    real = a.set_memory_limit("backend", "backend", 192 * MI, 512 * MI, dry_run=False)
    assert real.accepted and real.changed and api.patches[1][4] == {"_content_type": SMP}   # no dry run


def test_compare_and_set_refuses_without_writing():
    a, api = actuator()
    out = a.set_memory_limit("backend", "backend", 256 * MI, 512 * MI, dry_run=False)
    assert not out.accepted and not out.changed and "compare-and-set" in out.error and api.patches == []
    out = a.set_replicas("postgres", 2, 1, dry_run=False)
    assert not out.accepted and api.patches == []


def test_replicas_and_configuration_map_to_their_fixed_operations():
    a, api = actuator()
    assert a.set_replicas("postgres", 0, 1, dry_run=True).after == "1"
    assert api.patches[-1][3] == {"metadata": {"resourceVersion": "41"}, "spec": {"replicas": 1}}
    out = a.set_config_value("configmap/backend-config", "DATABASE_URL", "postgresql://shop@postgres-wrong:5432/shop",
                             "postgresql://shop@postgres:5432/shop", "backend", dry_run=False)
    assert out.accepted and out.changed and len(out.detail) == 2
    cm, dep = api.patches[-2], api.patches[-1]
    assert cm[0] == "ConfigMap" and cm[3] == {"metadata": {"resourceVersion": "7"},
                                              "data": {"DATABASE_URL": "postgresql://shop@postgres:5432/shop"}}
    assert dep[0] == "Deployment" and RESTART_ANNOTATION in dep[3]["spec"]["template"]["metadata"]["annotations"]


def test_a_restart_failure_after_the_config_write_is_reported_as_a_partial_change():
    a, api = actuator(FakeAPI(fail_second_patch=True))
    out = a.set_config_value("configmap/backend-config", "DATABASE_URL", "postgresql://shop@postgres-wrong:5432/shop",
                             "postgresql://shop@postgres:5432/shop", "backend", dry_run=False)
    assert not out.accepted and out.changed and "unavailable" in out.error


def test_unsupported_sources_are_rejected():
    a, api = actuator()
    out = a.set_config_value("secret/postgres-credentials", "X", "a", "b", "backend", dry_run=True)
    assert not out.accepted and "unsupported" in out.error and api.patches == []
