"""Offline benchmark: the deterministic investigator on every simulated scenario, both collection strategies.
Measures the diagnosis (category, root-cause component, confidence), evidence collected, capability calls, the
remediation plan's first action, and time. No cluster, no network, no LLM."""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "tests"), ROOT]

import fake_cluster as fc  # noqa: E402
from test_scenarios import run  # noqa: E402

from investigator.diagnosis import diagnose  # noqa: E402
from investigator.remediation import plan_remediation  # noqa: E402

# (scenario, expected category, expected root-cause component or None)
CASES = [
    ("base_world", "undetermined", None, "healthy cluster (no failure)"),
    ("world_oom", "memory_exhaustion", "backend", "OOMKilled at the memory limit"),
    ("world_db_misconfig", "dependency_misconfiguration", None, "wrong database host in configuration"),
    ("world_db_down", "dependency_unavailable", "postgres", "PostgreSQL scaled to 0"),
    ("world_crash", "application_crash", "backend", "application crash (unhandled exception)"),
    ("world_crash_caused_by_db_down", "dependency_unavailable", "postgres", "crash caused by the database being down"),
    ("world_liveness_kill", "health_check_failure", None, "exit 137 from a failed liveness probe (looks like OOM)"),
    ("world_db_down_after_config_edit", "dependency_unavailable", None, "database down after an unrelated config edit"),
    ("world_healthy_after_rollout", "undetermined", None, "normal rollout, briefly not ready"),
    ("world_oom_backend_dead", "memory_exhaustion", "backend", "OOM with the backend completely down"),
]

rows = []
for name, want_cat, want_comp, what in CASES:
    world_fn = getattr(fc, name)
    for strategy in ("planned", "exhaustive"):
        t0 = time.perf_counter()
        dx, store = run(world_fn(), strategy)
        ms = (time.perf_counter() - t0) * 1000
        facts = len(store.facts) if hasattr(store, "facts") else None
        steps = store.trace
        calls = sum(1 for s in steps if s.get("step") == "capability")
        comp = (dx.get("root_cause_component") or {}).get("name") if isinstance(dx.get("root_cause_component"), dict) else dx.get("root_cause_component")
        ok = dx.get("category") == want_cat and (want_comp is None or comp == want_comp)
        rows.append({"scenario": name, "what": what, "strategy": strategy, "expected": want_cat,
                     "category": dx.get("category"), "component": comp, "confidence": dx.get("confidence"),
                     "correct": ok, "facts": facts, "capability_calls": calls, "ms": round(ms, 1)})
print(json.dumps(rows, indent=1, default=str))
