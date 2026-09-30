"""Similar-symptom tests: evidence must rule out the plausible-looking alternative (docs/INCIDENTS.md
"Testing Rules": ambiguous/similar symptoms, evidence that rules out plausible alternatives)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fake_cluster import (world_crash_caused_by_db_down, world_db_down_after_config_edit,  # noqa: E402
                          world_healthy_after_rollout, world_liveness_kill)
from test_scenarios import run  # noqa: E402

STRATEGIES = ("planned", "exhaustive")


def rejected(dx, category, component="backend"):
    return next((a for a in dx["alternatives"] if a["category"] == category and a["component"] == component), None)


def test_crash_loop_caused_by_missing_database_is_not_an_app_crash():
    for s in STRATEGIES:
        dx, _ = run(world_crash_caused_by_db_down(), s)
        assert dx["category"] == "dependency_unavailable", (s, dx["category"])
        assert dx["root_cause_component"]["name"] == "postgres", s
        crash = rejected(dx, "application_crash")
        assert crash and "dependency connection error" in crash["why_not"], (s, crash)


def test_exit_137_from_a_liveness_kill_is_not_memory_exhaustion():
    for s in STRATEGIES:
        dx, _ = run(world_liveness_kill(), s)
        assert dx["category"] == "health_check_failure", (s, dx["category"])
        assert dx["category"] != "memory_exhaustion"
        mem = rejected(dx, "memory_exhaustion")
        assert mem is not None and mem["score"] < 0.3, (s, mem)


def test_unrelated_config_edit_does_not_turn_an_outage_into_a_misconfiguration():
    for s in STRATEGIES:
        dx, _ = run(world_db_down_after_config_edit(), s)
        assert dx["category"] == "dependency_unavailable", (s, dx["category"])
        mis = rejected(dx, "dependency_misconfiguration")
        assert mis and "existing Service" in mis["why_not"], (s, mis)


def test_rollout_noise_on_a_healthy_system_is_not_an_incident():
    for s in STRATEGIES:
        dx, _ = run(world_healthy_after_rollout(), s)
        assert dx["category"] == "undetermined", (s, dx["category"])
