"""Kubernetes actuator (Iteration 7): the ONLY code that writes to the cluster.

Three typed operations, each mapped to one fixed Kubernetes API operation (plus a fixed restart for a configuration
change). Nothing else can be expressed: no kubectl, no exec, no YAML, no caller-supplied patch.

  set_memory_limit  -> PATCH Deployment: spec.template.spec.containers[name].resources.limits.memory
  set_replicas      -> PATCH Deployment: spec.replicas
  set_config_value  -> PATCH ConfigMap: data[item], then PATCH Deployment: the pod-template restart annotation
                       (what `kubectl rollout restart` does), so the consumer reads the value again

Each write is compare-and-set twice over: the live value read just before must equal `expected`, and the patch
carries that object's resourceVersion, so the API server rejects it (409) if the object changed in between.
`dry_run=True` sends the same request with dryRun=All: validated and admitted by the API server, never persisted.
"""
from datetime import datetime, timezone

from ..execution_model import memory_bytes, mib
from .base import Actuator, ChangeResult

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


def _quantity(b: float) -> str:
    return mib(b)


def _err(exc: Exception) -> str:
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None) or type(exc).__name__
    body = str(getattr(exc, "body", "") or "")
    msg = ""
    if '"message":' in body:
        msg = body.split('"message":', 1)[1].split('",', 1)[0].strip(' "')[:300]
    return f"HTTP {status} {reason}" + (f": {msg}" if msg else "") if status else f"{reason}: {str(exc)[:300]}"


class KubernetesActuator(Actuator):
    name = "kubernetes"

    def __init__(self, kube, namespace: str):
        self.kube, self.ns, self.scope = kube, namespace, namespace

    def _kw(self, dry_run: bool) -> dict:
        # The client would label a dict body as the first listed type (a JSON Patch); these are strategic-merge
        # patches (containers merged by name), so the content type is always stated explicitly.
        kw = {"_content_type": "application/strategic-merge-patch+json"}
        return {**kw, "dry_run": "All"} if dry_run else kw

    # ----------------------------------------------------------------------------- memory limit
    def set_memory_limit(self, component, process, expected_bytes, new_bytes, dry_run) -> ChangeResult:
        target = f"Deployment {self.ns}/{component} container {process}"
        res = ChangeResult(False, dry_run, "set_memory_limit", target)
        try:
            d = self.kube.apps.read_namespaced_deployment(component, self.ns)
            c = next((c for c in d.spec.template.spec.containers if c.name == process), None)
            if c is None:
                res.error = f"container {process} not found"
                return res
            live = memory_bytes(((c.resources and c.resources.limits) or {}).get("memory"))
            res.before = mib(live) if live else None
            if live != expected_bytes:
                res.error = f"compare-and-set: live limit {res.before} is not the expected {mib(expected_bytes)}"
                return res
            body = {"metadata": {"resourceVersion": d.metadata.resource_version},
                    "spec": {"template": {"spec": {"containers": [
                        {"name": process, "resources": {"limits": {"memory": _quantity(new_bytes)}}}]}}}}
            out = self.kube.apps.patch_namespaced_deployment(component, self.ns, body, **self._kw(dry_run))
            oc = next(c for c in out.spec.template.spec.containers if c.name == process)
            res.after = (oc.resources.limits or {}).get("memory")
            res.detail.append(f"PATCH {target} resources.limits.memory={_quantity(new_bytes)}"
                              + (" (dry run)" if dry_run else ""))
            res.accepted, res.changed = True, not dry_run
        except Exception as exc:  # noqa: BLE001 - reported, never retried
            res.error = _err(exc)
        return res

    # ----------------------------------------------------------------------------- replicas
    def set_replicas(self, component, expected, new, dry_run) -> ChangeResult:
        target = f"Deployment {self.ns}/{component}"
        res = ChangeResult(False, dry_run, "set_replicas", target)
        try:
            d = self.kube.apps.read_namespaced_deployment(component, self.ns)
            live = d.spec.replicas if d.spec.replicas is not None else 1
            res.before = str(live)
            if live != expected:
                res.error = f"compare-and-set: live replicas {live} is not the expected {expected}"
                return res
            body = {"metadata": {"resourceVersion": d.metadata.resource_version}, "spec": {"replicas": int(new)}}
            out = self.kube.apps.patch_namespaced_deployment(component, self.ns, body, **self._kw(dry_run))
            res.after = str(out.spec.replicas)
            res.detail.append(f"PATCH {target} spec.replicas={int(new)}" + (" (dry run)" if dry_run else ""))
            res.accepted, res.changed = True, not dry_run
        except Exception as exc:  # noqa: BLE001
            res.error = _err(exc)
        return res

    # ----------------------------------------------------------------------------- configuration value + restart
    def set_config_value(self, source, item, expected_value, new_value, restart_component, dry_run) -> ChangeResult:
        kind, _, name = source.partition("/")
        target = f"ConfigMap {self.ns}/{name} key {item}, then Deployment {self.ns}/{restart_component} restart"
        res = ChangeResult(False, dry_run, "set_config_value", target)
        if kind != "configmap" or not name:
            res.error = f"unsupported configuration source {source}"
            return res
        try:
            cm = self.kube.core.read_namespaced_config_map(name, self.ns)
            live = (cm.data or {}).get(item)
            res.before = live
            if live != expected_value:
                res.error = f"compare-and-set: {item} changed since the recheck"
                return res
            d = self.kube.apps.read_namespaced_deployment(restart_component, self.ns)
            cm_body = {"metadata": {"resourceVersion": cm.metadata.resource_version}, "data": {item: new_value}}
            out = self.kube.core.patch_namespaced_config_map(name, self.ns, cm_body, **self._kw(dry_run))
            res.after = (out.data or {}).get(item)
            res.detail.append(f"PATCH ConfigMap {self.ns}/{name} data.{item}" + (" (dry run)" if dry_run else ""))
            res.changed = not dry_run
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            d_body = {"metadata": {"resourceVersion": d.metadata.resource_version},
                      "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: stamp}}}}}
            self.kube.apps.patch_namespaced_deployment(restart_component, self.ns, d_body, **self._kw(dry_run))
            res.detail.append(f"PATCH Deployment {self.ns}/{restart_component} template annotation "
                              f"{RESTART_ANNOTATION}={stamp}" + (" (dry run)" if dry_run else ""))
            res.accepted = True
        except Exception as exc:  # noqa: BLE001 - if the ConfigMap was written but the restart failed, changed=True
            res.error = _err(exc)
        return res
