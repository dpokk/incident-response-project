"""Timeline integrity: the investigator must represent uncertainty, never manufacture precision
(docs/ARCHITECTURE.md §7). An event that began before the investigation window keeps its real first
timestamp and is marked `before_window`; it is never re-dated to the window start."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fake_cluster import NOW, base_world, world_db_down  # noqa: E402
from test_scenarios import run  # noqa: E402

from investigator.report import build, hms, render_text  # noqa: E402

WINDOW_START = NOW - 330  # the window test_scenarios.run() investigates


def world_with_old_event():
    w = world_db_down()
    w["events"].append({"type": "Warning", "reason": "Unhealthy", "message": "Readiness probe failed: timeout",
                        "object_kind": "Pod", "object_name": "backend-11111", "count": 40,
                        "first": NOW - 7200, "last": NOW - 20})
    return w


def test_pre_window_event_is_not_redated():
    _, store = run(world_with_old_event(), "exhaustive")
    ev = next(f for f in store.facts if f.kind == "event" and f.data["reason"] == "Unhealthy")
    assert ev.t_basis == "before_window"
    assert ev.data["first_seen"] == NOW - 7200            # the source's own timestamp is kept
    assert ev.t == NOW - 20 and ev.t != WINDOW_START       # placed at its observation, not the window edge
    assert "before the investigation window" in ev.text and "count includes earlier occurrences" in ev.text


def test_in_window_events_stay_exact():
    _, store = run(world_db_down(), "exhaustive")
    assert all(f.t_basis == "exact" for f in store.facts if f.kind == "event")


def test_report_labels_pre_window_entries():
    dx, store = run(world_with_old_event(), "exhaustive")
    incident = {"id": "INC-TEST", "detected_at": NOW - 30, "namespace": "shop", "signals": []}
    r = build(incident, store, dx, (WINDOW_START, NOW), True)
    first = r["timeline"][0]
    assert first["t_basis"] == "before_window"
    text = render_text(r)
    assert "before window" in text
    assert f"{hms(WINDOW_START)}  Warning event Unhealthy" not in text


def test_healthy_cluster_has_no_before_window_noise():
    _, store = run(base_world(), "exhaustive")
    assert not [f for f in store.facts if f.t_basis == "before_window"]
