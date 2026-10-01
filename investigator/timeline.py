"""Incident timeline reconstruction (Iteration 4). Provider-independent.

Three layers, kept apart:

  observed entries  facts with a time, each given a *role* (change, precursor, symptom, failure, context) and a
                    time *interval* taken from its time basis - never more precise than the source:
                      exact          [t, t]
                      bounded        [t_earliest, t_latest]
                      observed / before_window   (unknown, t]   - happened at or before t
                      unknown        not placed in the sequence at all
  phases (inferred) baseline -> onset -> development -> failure -> recovery -> post-incident, each citing the
                    facts that bound it
  relations         "A preceded B" only when A's interval ends before B's begins; otherwise the order is stated
  (inferred)        as undetermined. Relations state order, never cause: causation is the diagnosis' job.

It answers: what changed first, when symptoms began, when the failure occurred, what happened immediately
before it, and what happened after recovery. It reads facts and the diagnosis' choice of components only;
it adds no facts and changes no conclusions.
"""
from datetime import datetime

from .evidence import EvidenceStore, Fact

BEFORE_FAILURE_S = 120     # "immediately before the failure"
EDGE_S = 30                # onset this close to the window start: the incident may have begun earlier

CHANGE_EVENTS = {"scaled", "instance_deleted", "stopped"}
FAILURE_EVENTS = {"memory_limit", "killed_by_health_check", "failed", "evicted"}
SYMPTOM_EVENTS = {"restart_backoff", "health_check_failed", "scheduling_failed", "node_problem"}
PRECURSOR_SIGNATURES = {"memory_pressure"}
PHASES = ("baseline", "onset", "development", "failure", "recovery", "post_incident")


def hms(t) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "?"


# --------------------------------------------------------------------------- observed entries

def role(f: Fact) -> str | None:
    """What part an observation can play in an incident's story (None = not a timeline entry)."""
    k, d = f.kind, f.data
    if k in ("change", "configuration_change", "config_changed"):
        return "change"
    if k == "event":
        cat = d.get("category")
        return ("change" if cat in CHANGE_EVENTS else "failure" if cat in FAILURE_EVENTS
                else "symptom" if cat in SYMPTOM_EVENTS else "context")
    if k == "process_terminated":
        return "context" if d.get("cause") == "completed" else "failure"
    if k == "log_exception":
        return "failure"
    if k == "log_signature":
        return "precursor" if d.get("signature") in PRECURSOR_SIGNATURES else "symptom"
    if k in ("metric_traffic_change", "metric_memory_high"):
        return "precursor"
    if k in ("metric_error_ratio", "detection_signal"):
        return "symptom"
    if k in ("log_tail_before_exit", "past_instance"):
        return "context"
    return None


def interval(f: Fact) -> tuple[float | None, float | None] | None:
    """When the observed thing began, as (earliest, latest). None = no time known."""
    if f.t is None or f.t_basis == "unknown":
        return None
    if f.t_basis == "bounded":
        return (f.t_earliest, f.t_latest if f.t_latest is not None else f.t)
    if f.t_basis == "before_window":
        # A recurring observation aggregated by the source: its occurrences lie somewhere between its first and
        # last sighting, and the first may belong to an earlier episode. Only that range is known.
        return (f.data.get("first_seen"), f.t)
    if f.t_basis == "observed":
        return (None, f.t)
    return (f.t, f.t)


def component_of(f: Fact) -> str | None:
    kind, _, name = f.subject.partition("/")
    if kind in ("component", "service"):
        return name
    return f.data.get("consumer") or f.data.get("component")


def _key(e: dict) -> float:
    lo, hi = e["interval"]
    return hi if lo is None else lo


def precedes(a: dict, b: dict) -> bool | None:
    """True: a certainly began before b. False: b certainly began first. None: cannot tell."""
    (alo, ahi), (blo, bhi) = a["interval"], b["interval"]
    if blo is not None and ahi is not None and ahi < blo:
        return True
    if alo is not None and bhi is not None and bhi < alo:
        return False
    return None


# --------------------------------------------------------------------------- reconstruction

def reconstruct(store: EvidenceStore, dx: dict, window: tuple) -> dict:
    start, end = window
    affected = dx.get("affected_component")
    rcc = (dx.get("root_cause_component") or {})
    focus = {affected} | ({rcc["name"]} if rcc.get("kind") == "component" else set())
    focus.discard(None)
    impacted = set(dx.get("impacted_components") or [])

    entries, undated = [], []
    for f in store.facts:
        r = role(f)
        if r is None:
            continue
        iv = interval(f)
        e = {"id": f.id, "role": r, "component": component_of(f), "text": f.text, "t": f.t, "t_basis": f.t_basis,
             "origin": f.origin, "interval": iv, "kind": f.kind, "category": f.data.get("category")}
        if iv is None:
            undated.append(e)
        elif r != "change" and (iv[1] is not None and iv[1] < start - 600):
            continue                     # long before the window and not a change: not part of this incident
        else:
            entries.append(e)
    entries.sort(key=_key)

    def first(pred):
        """Earliest entry matching pred, among entries that can anchor a point in this incident: an observation
        recurring since before the window cannot (its first sighting may belong to an earlier episode)."""
        return next((e for e in entries if e["t_basis"] != "before_window" and pred(e)), None)

    recurring = [e for e in entries if e["t_basis"] == "before_window" and e["role"] in ("symptom", "failure", "change")]

    failures = [e for e in entries if e["role"] == "failure" and e["component"] in focus and e["t_basis"] != "before_window"]
    first_failure = failures[0] if failures else None
    last_failure = max(failures, key=lambda e: e["interval"][1] or 0) if failures else None
    failure_shown_by_symptom = False
    if first_failure is None:            # e.g. a dependency outage: the failure shows as the affected component's errors
        first_failure = first(lambda e: e["role"] == "symptom" and e["component"] == affected)
        last_failure = first_failure
        failure_shown_by_symptom = first_failure is not None
    onset = first(lambda e: e["role"] in ("precursor", "symptom", "failure"))
    first_symptom = first(lambda e: e["role"] == "symptom")
    changes = [e for e in entries if e["role"] == "change" and e["t_basis"] != "before_window"]
    prior_changes = [c for c in changes if onset is None or precedes(c, onset) is not False]
    later_changes = [c for c in changes if last_failure is not None and precedes(last_failure, c) is True]
    first_impact = first(lambda e: e["component"] in impacted and e["role"] in ("symptom", "failure"))

    recovery = _recovery(store, focus, last_failure, end)
    uncertain: list[str] = _unrecorded_failures(store, focus, entries, first_failure)
    phases = _phases(entries, onset, first_failure, last_failure, recovery, prior_changes, store, focus,
                     (start, end), uncertain)
    relations = _relations(prior_changes[-1] if prior_changes else None, onset, first_failure, first_impact,
                           last_failure, recovery)

    before = []
    if first_failure is not None:
        ref = first_failure["interval"][0] if first_failure["interval"][0] is not None else first_failure["interval"][1]
        before = [e for e in entries if e is not first_failure and e["interval"][1] is not None
                  and ref - BEFORE_FAILURE_S <= e["interval"][1] <= ref
                  and (e["component"] in focus or e["role"] in ("change", "precursor"))]
        before += [e for e in entries if e["kind"] == "log_tail_before_exit" and e["component"] in focus
                   and e not in before and e["interval"][1] is not None and e["interval"][1] <= ref + 1]
    after = [e for e in entries if recovery and recovery.get("t") and e["interval"][0] is not None
             and e["interval"][0] >= recovery["t"]]

    if onset is not None and (onset["interval"][0] is None or onset["interval"][0] - start <= EDGE_S):
        uncertain.append(f"The earliest sign of the incident ({onset['id']}) is at or before the start of the "
                         f"investigation window ({hms(start)}): the incident may have begun earlier than the evidence shows")
    if recurring:
        uncertain.append(f"{len(recurring)} observation(s) were already recurring before the window ("
                         + ", ".join(e["id"] for e in recurring[:6]) + "): the source aggregates repeats, so which "
                         "occurrences belong to this incident cannot be told; they are not used as its start")
    if undated:
        uncertain.append(f"{len(undated)} observation(s) have no known time and are not placed in the sequence: "
                         + ", ".join(e["id"] for e in undated[:8]))

    answers = {
        "what_changed_first": _answer_change(prior_changes, onset, store, start),
        "symptoms_began": _answer_at(first_symptom, "Symptoms were first observed"),
        "failure_occurred": _answer_at(first_failure, f"The failure first showed in {affected}'s errors"
                                       if failure_shown_by_symptom else "The earliest recorded failure was"),
        "immediately_before_failure": {"facts": [e["id"] for e in before],
                                       "statement": (f"{len(before)} observation(s) in the {BEFORE_FAILURE_S}s before "
                                                     f"the first failure" if before else
                                                     "Nothing was recorded immediately before the first failure")},
        "after_recovery": {"facts": [e["id"] for e in after],
                           "statement": (recovery["statement"] if recovery else
                                         "No recovery was observed in the evidence")},
    }
    onset_t = None if onset is None else (onset["interval"][0] or onset["interval"][1])
    return {"entries": [{k: v for k, v in e.items() if k != "interval"} | {"earliest": e["interval"][0],
                         "latest": e["interval"][1]} for e in entries],
            "undated": [e["id"] for e in undated], "phases": phases, "relations": relations,
            "answers": answers, "uncertainties": uncertain,
            # Changes after the last failure (e.g. a restart or rollout by an operator): part of the story, but they
            # came after the failure, so they cannot have started it.
            "changes_after_failure": [{"id": c["id"], "text": c["text"]} for c in later_changes],
            "window": {"start": start, "end": end,
                       "evidence_start": min((_key(e) for e in entries if e["t_basis"] != "before_window"), default=None),
                       "evidence_end": max((e["interval"][1] for e in entries if e["interval"][1]), default=None),
                       "incident_start": onset_t,
                       "incident_end": recovery.get("t") if recovery and recovery.get("recovered") else None,
                       "ongoing": bool(recovery) and recovery.get("recovered") is False}}


def _recovery(store: EvidenceStore, focus: set, last_failure: dict | None, end: float) -> dict | None:
    """Recovery is claimed only when, for every failing component, all instances are ready, each became ready
    after the last failure (the source's own `ready_since`), and its errors have stopped. Otherwise the report
    says it had not recovered, or that when it recovered is unknown."""
    if last_failure is None or last_failure["interval"][1] is None:
        return None
    after_t = last_failure["interval"][1]
    times, facts, unknown = [], [], []
    for comp in sorted(focus):
        status = next(iter(store.find(kind="component_status", subject=f"component/{comp}")), None)
        insts = store.find(kind="instance_status", subject=f"component/{comp}")
        if status is None:
            continue
        facts += [status.id] + [i.id for i in insts]
        not_ready = [i for i in insts if not i.data.get("ready")]
        if status.data.get("desired", 0) == 0 or not insts or not_ready or \
                status.data.get("ready", 0) < status.data.get("desired", 0):
            why = (f"{len(not_ready)} of {len(insts)} instance(s) not ready" if not_ready
                   else "no instance is running" if not insts or status.data.get("desired", 0) == 0 else status.text)
            return {"t": None, "facts": [status.id] + [i.id for i in not_ready][:3], "recovered": False,
                    "statement": f"{comp} had not recovered when the evidence was collected ({why})"}
        ongoing = [s for s in store.find(kind="log_signature", subject=f"component/{comp}")
                   if s.data.get("signature") not in PRECURSOR_SIGNATURES and (s.data.get("last") or 0) >= end - EDGE_S]
        if ongoing:
            return {"t": None, "facts": [ongoing[0].id], "recovered": False,
                    "statement": f"{comp} instances are ready but it was still logging errors at "
                                 f"{hms(ongoing[0].data['last'])}: not recovered"}
        since = [i.data.get("ready_since") for i in insts]
        if any(s is None or s <= after_t for s in since):
            unknown.append(comp)
        else:
            times.append(max(since))
    if not facts:
        return None
    if unknown:
        return {"t": None, "facts": facts, "recovered": None,
                "statement": f"{', '.join(unknown)} ready now, but when it became ready again after the failure is "
                             f"not known from the evidence"}
    t = max(times)
    return {"t": t, "facts": facts, "recovered": True,
            "statement": f"All instances of {', '.join(sorted(focus))} were ready again by {hms(t)}, and no errors "
                         f"were logged after that"}


def _unrecorded_failures(store: EvidenceStore, focus: set, entries: list, first_failure: dict | None) -> list[str]:
    """Signs that failures happened that the evidence does not contain: restarts outnumber recorded
    terminations, or the platform was already backing off restarts before the first recorded failure."""
    out = []
    for comp in sorted(focus):
        restarts = sum(i.data.get("restarts", 0) for i in store.find(kind="instance_status", subject=f"component/{comp}"))
        recorded = len(store.find(kind="process_terminated", subject=f"component/{comp}"))
        if restarts > recorded:
            out.append(f"{comp} restarted {restarts} time(s) but only {recorded} termination(s) are recorded: earlier "
                       f"failures happened that the evidence does not describe")
    if first_failure is not None:
        backoff = next((e for e in entries if e["category"] == "restart_backoff" and e["component"] in focus
                        and precedes(e, first_failure) is True), None)
        if backoff:
            out.append(f"Restarts were already being backed off ({backoff['id']}) before the earliest recorded failure "
                       f"({first_failure['id']}): the failure began earlier than the evidence shows")
    return out


def _phases(entries, onset, first_failure, last_failure, recovery, prior_changes, store, focus, window,
            uncertain) -> list[dict]:
    start, end = window
    out = []
    onset_t = None if onset is None else (onset["interval"][0] or onset["interval"][1])
    gaps = [g for g in store.find(kind="evidence_gap") if g.data.get("gap") in ("not_recorded", "no_history")]
    baseline_note = " (part of this period was not recorded; see evidence coverage)" if gaps else ""
    if onset is None:
        out.append({"phase": "baseline", "start": start, "end": end, "facts": [],
                    "statement": "No change, symptom or failure was found in the evidence window" + baseline_note})
        return out
    recurring = [e for e in entries if e["t_basis"] == "before_window" and e["role"] in ("symptom", "failure")]
    if onset_t is not None and onset_t < start:
        out.append({"phase": "baseline", "start": None, "end": None, "facts": [],
                    "statement": "The incident began before the investigation window: no baseline was examined"})
    else:
        out.append({"phase": "baseline", "start": start, "end": onset_t,
                    "facts": [c["id"] for c in prior_changes] + [e["id"] for e in recurring],
                    "statement": f"No new symptom or failure was recorded before {hms(onset_t)}" + baseline_note
                                 + (f"; {len(prior_changes)} change(s) were recorded" if prior_changes else "")
                                 + (f"; {len(recurring)} observation(s) recurring since before the window"
                                    if recurring else "")})
    out.append({"phase": "onset", "start": onset_t, "end": onset_t, "facts": [onset["id"]],
                "statement": f"First sign of the incident ({onset['role']}): {onset['text'][:140]}"})
    if first_failure is not None:
        ff_t = first_failure["interval"][0] or first_failure["interval"][1]
        dev = [e for e in entries if e["role"] in ("precursor", "symptom") and e is not onset
               and e["interval"][1] is not None and onset_t is not None and onset_t <= e["interval"][1] <= ff_t]
        if dev or (onset_t is not None and ff_t is not None and ff_t > onset_t):
            out.append({"phase": "development", "start": onset_t, "end": ff_t, "facts": [e["id"] for e in dev],
                        "statement": (f"{len(dev)} warning sign(s) and symptom(s) between the onset and the failure"
                                      if dev else "No further signs were recorded between the onset and the failure")})
        lf_t = last_failure["interval"][1] if last_failure else ff_t
        n = sum(1 for e in entries if e["role"] == "failure" and e["component"] in focus)
        out.append({"phase": "failure", "start": ff_t, "end": lf_t,
                    "facts": [e["id"] for e in entries if e["role"] == "failure" and e["component"] in focus][:10]
                    or [first_failure["id"]],
                    "statement": (f"{n} failure observation(s) in {', '.join(sorted(focus))} between {hms(ff_t)} "
                                  f"and {hms(lf_t)}" if n else
                                  f"The failure showed first as {first_failure['text'][:120]}")})
    if recovery is not None:
        out.append({"phase": "recovery", "start": recovery.get("t"), "end": recovery.get("t"), "facts": recovery["facts"],
                    "statement": recovery["statement"]})
    elif first_failure is not None:
        out.append({"phase": "recovery", "start": None, "end": None, "facts": [],
                    "statement": "No recovery was observed in the evidence"})
    status = [s for c in sorted(focus) for s in store.find(kind="component_status", subject=f"component/{c}")]
    if status:
        out.append({"phase": "post_incident", "start": end, "end": end, "facts": [s.id for s in status],
                    "statement": "State when the evidence was collected: " + "; ".join(s.text for s in status)})
    return out


def _relation(a, b, label_a, label_b) -> dict | None:
    if a is None or b is None or a is b:
        return None
    order = precedes(a, b)
    if order is True:
        gap = (b["interval"][0] or b["interval"][1]) - (a["interval"][1] or a["interval"][0])
        stmt = f"{label_a} ({a['id']}) preceded {label_b} ({b['id']}) by {gap:.0f}s"
    elif order is False:
        stmt = f"{label_b} ({b['id']}) came before {label_a} ({a['id']})"
    else:
        stmt = (f"The order of {label_a} ({a['id']}) and {label_b} ({b['id']}) cannot be determined from their "
                f"timestamps")
    return {"from": a["id"], "to": b["id"], "order": {True: "before", False: "after", None: "undetermined"}[order],
            "statement": stmt + " (order only, not cause)"}


def _relations(last_change, onset, first_failure, first_impact, last_failure, recovery) -> list[dict]:
    rec = None
    if recovery and recovery.get("t"):
        rec = {"id": recovery["facts"][0], "interval": (recovery["t"], recovery["t"])}
    rels = [_relation(last_change, onset, "the last recorded change", "the onset"),
            _relation(onset, first_failure, "the onset", "the first failure"),
            _relation(first_failure, first_impact, "the first failure", "the first impact on a caller"),
            _relation(last_failure, rec, "the last failure", "recovery")]
    return [r for r in rels if r]


def _answer_change(prior_changes, onset, store, start) -> dict:
    """The first change in the lead-up to the incident (inside the investigated window). If none, the most
    recent earlier change, with how long before the onset it was - an old change is reported as such."""
    gaps = store.find(kind="evidence_gap")
    if not prior_changes:
        return {"facts": [], "statement": "No change was recorded before the onset"
                + (" (the evidence has gaps, so an unrecorded change cannot be ruled out)" if gaps else "")}
    lead_up = [c for c in prior_changes if c["interval"][1] is not None and c["interval"][1] >= start]
    c = lead_up[0] if lead_up else prior_changes[-1]
    when = f"by {hms(c['interval'][1])}" if c["t_basis"] in ("bounded", "observed") else f"at {hms(c['interval'][0])}"
    if lead_up:
        return {"facts": [c["id"]], "statement": f"The first recorded change in the lead-up ({when}): {c['text'][:160]}"}
    onset_t = None if onset is None else (onset["interval"][0] or onset["interval"][1])
    ago = f", {(onset_t - c['interval'][1]) / 60:.0f} min before the onset" if onset_t and c["interval"][1] else ""
    return {"facts": [c["id"]], "statement": f"No change inside the investigated window; the most recent earlier change "
                                             f"({when}{ago}): {c['text'][:140]}"}


def _answer_at(e, prefix) -> dict:
    if e is None:
        return {"facts": [], "statement": f"{prefix}: not found in the evidence"}
    lo, hi = e["interval"]
    when = f"at {hms(lo)}" if lo is not None and lo == hi else \
        f"between {hms(lo)} and {hms(hi)}" if lo is not None else f"at or before {hms(hi)}"
    return {"facts": [e["id"]], "statement": f"{prefix} {when}: {e['text'][:160]}"}
