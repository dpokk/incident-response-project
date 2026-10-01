"""Impact assessment (Iteration 4). Provider-independent, evidence-backed, deliberately basic.

Answers, from facts only: which instances of the affected component were affected, what state its
dependencies were in, which callers were impacted, whether the impact stayed isolated or propagated, how long
it lasted where that is known, and how many requests failed where metrics make it measurable.

Rules:
  * no number without a source fact; an estimate says it is one and how it was made;
  * "healthy" only with evidence of health; otherwise "not examined" or "unknown";
  * what could not be quantified is stated, not omitted;
  * root cause, affected component, impacted components and blast radius stay separate fields.
"""
from datetime import datetime

from .evidence import EvidenceStore
from .timeline import duration, exact, fmt_moment, moment


def hms(t) -> str:
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else "?"


def assess(store: EvidenceStore, dx: dict, reconstruction: dict | None) -> dict:
    affected = dx.get("affected_component")
    if dx.get("category") == "undetermined" or not affected:
        return {"assessed": False, "statement": "No impact assessment: the failure was not determined",
                "limitations": []}
    limitations: list[str] = []
    window = (reconstruction or {}).get("window") or {}
    failure_t = _failure_start(store, reconstruction)
    instances = _instances(store, affected, window.get("end"))
    deps = _dependencies(store, affected, dx, limitations)
    impacted = _impacted(store, dx)
    err, unattributed = _error_episode(store, failure_t)
    if unattributed:
        limitations.append(f"{len(unattributed)} user-facing error episode(s) in the window ended more than a minute "
                           f"before this failure began ({', '.join(f.id for f in unattributed)}): not counted as its "
                           f"impact")
    users = _users(store, limitations, err)
    duration = _duration(store, reconstruction, users, limitations, err)

    if impacted or users["affected"] is True:
        propagation = "propagated"
        prop_stmt = "The failure propagated beyond " + affected + ": " + ", ".join(
            [i["component"] for i in impacted] + (["users (entry point)"] if users["affected"] is True else []))
    elif users["affected"] is False and not impacted:
        propagation = "isolated"
        prop_stmt = f"No impact beyond {affected} was observed: callers and the user entry point show no failures"
    else:
        propagation = "unknown"
        prop_stmt = "Whether the failure reached callers or users cannot be told from the evidence"

    rcc = dx.get("root_cause_component") or {}
    blast = [{"component": rcc["name"], "role": "root cause"}] if rcc.get("kind") == "component" and \
        rcc.get("name") != affected else []
    blast += [{"component": affected, "role": "affected"}]
    blast += [{"component": i["component"], "role": "impacted"} for i in impacted]
    if users["affected"] is True:
        blast.append({"component": f"users (via {users['entry']})" if users.get("entry") else "user entry point",
                      "role": "users"})
    gaps = {f.data.get("gap") for f in store.find(kind="evidence_gap")}
    if "no_history" in gaps:
        limitations.append("No evidence history was retained for this window: impact on instances that restarted "
                           "more than once or no longer exist may be missing")
    elif gaps:
        limitations.append("Parts of the window had no retained evidence history (see evidence coverage), so impact "
                           "in those parts may be missing")
    return {"assessed": True, "affected_component": affected, "instances": instances, "dependencies": deps,
            "impacted": impacted, "users": users, "duration": duration, "propagation": propagation,
            "propagation_statement": prop_stmt, "blast_radius": blast, "limitations": limitations}


def _failure_start(store: EvidenceStore, rc: dict | None) -> float | None:
    ids = ((rc or {}).get("answers", {}).get("failure_occurred") or {}).get("facts") or []
    f = store.by_id(ids[0]) if ids else None
    if f is None:
        return None
    return f.t_earliest if f.t_basis == "bounded" and f.t_earliest else f.t


def _error_episode(store: EvidenceStore, failure_t: float | None) -> tuple:
    """The user-facing error episode that belongs to this failure: one still going, or ending no earlier than a
    minute before the failure began. Episodes that had already ended are another incident's."""
    eps = store.find(kind="metric_error_ratio")
    if failure_t is None:
        return (eps[0] if eps else None), []
    mine = [e for e in eps if e.data.get("episode_end") is None or e.data["episode_end"] >= failure_t - 60]
    return (mine[0] if mine else None), [e for e in eps if e not in mine]


def _instances(store: EvidenceStore, comp: str, window_end: float | None = None) -> dict:
    subj = f"component/{comp}"
    # Instances created after the window are today's, not part of a past incident.
    current = {f.data["instance"]: f for f in store.find(kind="instance_status", subject=subj)
               if window_end is None or not f.data.get("created") or f.data["created"] <= window_end + 60}
    gone = {f.data["instance"]: f for f in store.find(kind="past_instance", subject=subj)}
    terms = store.find(kind="process_terminated", subject=subj)
    hit = {t.data["instance"] for t in terms if t.data.get("cause") != "completed"}
    hit |= {i for i, f in current.items() if not f.data.get("ready")}
    hit |= {f.data["instance"] for f in store.find(kind="process_waiting", subject=subj) if f.data.get("problematic")}
    known = set(current) | set(gone) | {t.data["instance"] for t in terms}
    facts = [t.id for t in terms[:4]] + [f.id for i, f in current.items() if not f.data.get("ready")][:4]
    if not known:
        return {"affected": None, "total": None, "facts": [], "statement": f"{comp}: instances unknown"}
    n_gone = len(known - set(current))
    gone_note = f" ({n_gone} of them no longer exist)" if n_gone else ""
    levels = next(iter(store.find(kind="log_levels", subject=subj)), None)
    errors = levels.data.get("errors", 0) if levels else 0
    k = len(hit & known)
    if k == 0 and errors:
        # Running and ready, yet failing its work: the failure is in what it does, not in its instances' lifecycle.
        return {"affected": len(known), "total": len(known), "gone": n_gone, "facts": [levels.id], "failing_requests": True,
                "statement": f"{comp}: all {len(known)} instance(s) kept running, but the component was failing "
                             f"({errors} error log lines)" + gone_note}
    return {"affected": k, "total": len(known), "gone": n_gone, "facts": facts,
            "statement": f"{comp}: {k}/{len(known)} instances terminated, restarting or not ready" + gone_note}


def _dependencies(store: EvidenceStore, comp: str, dx: dict, limitations: list) -> list[dict]:
    out = []
    for ref in store.find(kind="config_reference", subject=f"component/{comp}"):
        endpoint = f"{ref.data['host']}:{ref.data['port']}"
        subj = f"dependency/{endpoint}"
        eps = next(iter(store.find(kind="service_endpoints", subject=subj)), None)
        probe = next((p for p in store.find(kind="connectivity_probe", subject=subj)
                      if not p.data.get("alternative_for")), None)
        backing = store.find(kind="backing_component", subject=subj)
        name = backing[0].data["component"] if backing else ref.data["host"]
        missing = store.find(kind="service_lookup", subject=subj, found=False)
        outage = next(iter(store.find(kind="availability_outage", subject=subj)), None)
        if outage:
            now = ("healthy at investigation" if (probe and probe.data.get("tcp") == "ok")
                   or (eps and eps.data.get("ready", 0) > 0) else "still unavailable at investigation")
            out.append({"component": name, "endpoint": endpoint, "state": "unavailable during the incident",
                        "facts": [outage.id] + [x.id for x in (probe, eps) if x],
                        "statement": f"{name} ({endpoint}): unavailable during the incident (recorded history: "
                                     f"{outage.text.split(': ', 1)[-1]}); {now}"})
            continue
        if missing:
            state, facts = "does not exist", [missing[0].id] + [x.id for x in (probe,) if x]
        elif (probe and probe.data.get("tcp") == "ok") or (not probe and eps and eps.data.get("ready", 0) > 0):
            state, facts = "healthy", [x.id for x in (probe, eps) if x]
        elif (probe and probe.data.get("tcp") in ("refused", "timeout", "error")) or \
                (probe and probe.data.get("dns") == "error") or (eps and eps.data.get("ready") == 0):
            state, facts = "unavailable", [x.id for x in (probe, eps) if x]
        else:
            state, facts = "not examined", []
            limitations.append(f"The state of {name} ({endpoint}) was not examined")
        out.append({"component": name, "endpoint": endpoint, "state": state, "facts": facts,
                    "statement": f"{name} ({endpoint}): {state}"})
    return out


def _impacted(store: EvidenceStore, dx: dict) -> list[dict]:
    out = []
    for comp in dx.get("impacted_components") or []:
        subj = f"component/{comp}"
        sig = next((s for s in store.find(kind="log_signature", subject=subj)), None)
        levels = next(iter(store.find(kind="log_levels", subject=subj)), None)
        f = sig or levels
        out.append({"component": comp, "facts": [f.id] if f else [],
                    "statement": f"{comp}: " + (f"{sig.data['count']} {sig.data['signature'].replace('_', ' ')} "
                                                f"message(s) about its calls" if sig else
                                                "errors logged" if levels else "impacted (see diagnosis)")})
    return out


def _users(store: EvidenceStore, limitations: list, err=None) -> dict:
    out = {"peak_error_ratio": None, "failed_requests_estimate": None, "requests_estimate": None, "entry": None}
    return {**out, **_users_assessed(store, limitations, err)}


def _users_assessed(store: EvidenceStore, limitations: list, err) -> dict:
    probes = store.find(kind="entry_probe")
    failing_probe = next((p for p in probes if (p.data.get("status") or 0) >= 500 or p.data.get("status") == 0), None)
    signal = next((s for s in store.find(kind="detection_signal")
                   if s.data.get("signal") in ("entry_probe_failure", "http_5xx_ratio")), None)
    if err:
        est = err.data.get("estimate")
        entry = err.data.get("component")
        stmt = f"User requests at {entry} failed: error ratio peaked at {err.data['peak']:.0%}"
        if est:
            stmt += (f"; about {est['failed']:,} of {est['total']:,} requests failed between {hms(est['from'])} and "
                     f"{hms(est['to'])} (estimate from sampled request and error rates)")
        else:
            stmt += "; the number of failed requests could not be estimated (no matching request-rate samples)"
            limitations.append("Failed requests could not be counted: no request-rate samples matched the error ratio")
        return {"affected": True, "quantified": bool(est), "entry": entry, "facts": [err.id],
                "peak_error_ratio": err.data["peak"], "failed_requests_estimate": est["failed"] if est else None,
                "requests_estimate": est["total"] if est else None, "statement": stmt}
    if store.find(kind="metric_traffic_steady") or store.find(kind="metric_traffic_change"):
        if not failing_probe and not signal:
            return {"affected": False, "quantified": True, "facts": [], "statement":
                    "No user-facing errors: the error ratio at the entry point stayed below 5%"}
    if failing_probe or signal:
        f = failing_probe or signal
        limitations.append("Request-level impact could not be quantified: no request metrics were available")
        return {"affected": True, "quantified": False, "facts": [f.id],
                "statement": f"Users were affected ({f.text[:120]}), but how many requests failed could not be "
                             f"quantified from the available evidence"}
    if probes:
        # A live probe describes the moment of collection, not the incident: say when it was observed.
        impacted_errors = [f for f in store.find(kind="log_signature") if f.data.get("signature") == "upstream_failure"]
        stmt = (f"A synthetic user request succeeded at investigation time ({hms(probes[0].collected_at)}); whether user "
                f"requests failed during the incident was not measured")
        if impacted_errors:
            stmt += (f" (the entry path logged {sum(f.data.get('count', 0) for f in impacted_errors)} upstream failures "
                     f"during the window, so users may have been affected)")
            limitations.append("User impact during the incident could not be quantified: no request metrics were "
                               "queried, and the live probe only shows the state at investigation time")
        return {"affected": None if impacted_errors else False, "quantified": False,
                "facts": [probes[0].id] + [f.id for f in impacted_errors[:1]], "statement": stmt}
    limitations.append("User impact is unknown: no synthetic request and no request metrics")
    return {"affected": None, "quantified": False, "facts": [], "statement": "User impact unknown"}


def _duration(store: EvidenceStore, rc: dict | None, users: dict, limitations: list, err=None) -> dict:
    """Duration as bounds between two moments (start/end each {earliest, latest, basis}); never a single number
    unless both ends are exact."""
    w = (rc or {}).get("window") or {}
    start, end, ongoing = w.get("incident_start"), w.get("incident_end"), w.get("ongoing")
    starts_early = any("may have begun earlier" in u or "began earlier" in u for u in (rc or {}).get("uncertainties", []))
    out = {"start": start, "end": end, "ongoing": bool(ongoing), "min_s": None, "max_s": None}
    if start is None:
        out["statement"] = "Duration unknown: the start of the incident was not identified"
        limitations.append("Incident duration unknown")
        return out
    span = f"{fmt_moment(start)} to {fmt_moment(end)}" if end else f"from {fmt_moment(start)}"
    if end:
        d = duration(start, end, may_start_earlier=starts_early)
        out["statement"] = f"{d['statement']} ({span}, from the first sign to recovery)"
    elif ongoing and w.get("end"):
        d = duration(start, None, ongoing_at=w["end"])
        out["statement"] = f"{d['statement']} ({span})"
    else:
        d = {"min_s": None, "max_s": None}
        out["statement"] = f"started {fmt_moment(start)}; when it ended is not known from the evidence"
    out["min_s"], out["max_s"] = d["min_s"], d["max_s"]
    if err and err.data.get("episode_end"):
        up = (moment((err.t_earliest, err.t_latest or err.t), "bounded") if err.t_basis == "bounded"
              else moment((None, err.t), "observed") if err.t_basis == "observed" else exact(err.t))
        rec = next((r for r in store.find(kind="metric_error_ratio_recovered")
                    if r.data.get("episode") == err.data.get("episode")), None)
        down = moment((rec.t_earliest, rec.t_latest), "bounded") if rec and rec.t_basis == "bounded" \
            else exact(err.data["episode_end"])
        e = duration(up, down)
        out["user_facing_errors"] = (f"user-facing errors lasted {e['statement']} (error ratio above 5% from "
                                     f"{fmt_moment(up)} to {fmt_moment(down)}; times are bounded by metric samples)")
    return out
