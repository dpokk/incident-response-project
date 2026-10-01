"""Verification of an executed change (Iteration 7). Evidence, not assumptions: "the API accepted the change" is never
treated as "the incident is resolved".

The criteria are the plan's own (`action.verification`, checks named in the capability vocabulary by Iteration 5);
nothing here invents a success definition or a threshold. They are evaluated through the capability layer:

  settle       from the change until every component_ready subject is ready on `stable_samples` consecutive samples
               (a rollout under load can flap readiness), at most `settle_max_s`
  observation  a bounded window (`window_s`), sampled every `poll_s`

  component_ready         every sample: desired > 0, ready == desired, every instance ready
  dependency_available    every sample: the endpoint's service has >= the expected ready endpoints
  entry_requests_succeed  every sample: the synthetic user request returns a status below the expected bound
  no_new_terminations     no termination with the given cause in an instance started after the change
  no_dependency_errors    the subject logs no dependency/connection error (for the endpoint) during observation
  error_ratio_below       every metric point during observation (after the metric's own averaging lag) is below

Outcome: any criterion failed -> NOT_RESOLVED; every criterion evaluated and passed -> RESOLVED; otherwise
(something could not be evaluated, or there are no criteria) -> INCONCLUSIVE. Uncertainty never becomes success.
"""
import json
import time
from dataclasses import asdict, dataclass, field

from .capabilities import TimeRange
from .execution_model import Outcome
from .logparse import DEPENDENCY_SIGNATURES, classify, extract_target, parse_records

PER_SAMPLE = ("component_ready", "dependency_available", "entry_requests_succeed", "no_new_terminations")
START_SLACK_S = 5          # instances created this close before the change count as started by it (clock skew)


@dataclass
class CriterionResult:
    check: str
    subject: str
    statement: str
    expectation: dict
    status: str = "unknown"             # pass | fail | unknown
    samples: int = 0
    passed: int = 0
    failed: int = 0
    unknown: int = 0
    evidence: list[str] = field(default_factory=list)

    def record(self, ok: bool | None, note: str) -> None:
        self.samples += 1
        if ok is True:
            self.passed += 1
        elif ok is False:
            self.failed += 1
        else:
            self.unknown += 1
        if ok is not True or not self.evidence or self.evidence[-1] != note:
            if len(self.evidence) < 8:
                self.evidence.append(note)

    def conclude(self) -> None:
        self.status = "fail" if self.failed else ("pass" if self.passed and not self.unknown else "unknown")


@dataclass
class VerificationResult:
    outcome: Outcome
    reason: str
    criteria: list[CriterionResult]
    settle: dict
    window: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return d


class Verifier:
    def __init__(self, capabilities, clock=time.time, sleep=time.sleep, poll_s: float = 5,
                 entry: tuple | None = None, log=print):
        """`capabilities`: callable returning a fresh read-only Capabilities object (reads are not cached across
        samples). `entry`: (service, port, path) of the synthetic user request."""
        self.caps, self.clock, self.sleep, self.poll_s, self.entry, self.log = \
            capabilities, clock, sleep, poll_s, entry, log

    def run(self, criteria: list[dict], applied_at: float, settle_max_s: float, window_s: float,
            progress=None, stable_samples: int = 1) -> VerificationResult:
        results = [CriterionResult(c["check"], c["subject"], c["statement"], c.get("expectation") or {})
                   for c in criteria]
        ready_subjects = [r.subject for r in results if r.check == "component_ready"]

        # settle: let the change take effect (rollout, start-up) before judging it
        # settled = ready on stable_samples consecutive samples: a rollout under load can flap readiness
        settle_end = applied_at + settle_max_s
        settled, streak = not ready_subjects, 0
        while not settled and self.clock() < settle_end:
            caps = self.caps()
            streak = streak + 1 if all(self._ready(caps, s)[0] is True for s in ready_subjects) else 0
            settled = streak >= stable_samples
            if not settled:
                self.sleep(self.poll_s)
        settle = {"started_at": applied_at, "ended_at": self.clock(), "max_s": settle_max_s, "ready": settled,
                  "stable_samples": stable_samples}
        if progress:
            progress("observing", {"settle": settle, "window_s": window_s})

        # observation window
        start = self.clock()
        end = start + window_s
        while True:
            caps = self.caps()
            for r in results:
                if r.check in PER_SAMPLE:
                    r.record(*self._sample(caps, r, applied_at))
            if self.clock() >= end:
                break
            self.sleep(min(self.poll_s, max(0.0, end - self.clock())))
        caps = self.caps()
        for r in results:
            if r.check == "no_dependency_errors":
                r.record(*self._dependency_errors(caps, r, start, self.clock()))
            elif r.check == "error_ratio_below":
                r.record(*self._error_ratio(caps, r, start, self.clock()))
            elif r.check not in PER_SAMPLE:
                r.record(None, f"check '{r.check}' cannot be evaluated by this verifier")
            r.conclude()
        window = {"started_at": start, "ended_at": self.clock(), "window_s": window_s, "poll_s": self.poll_s}
        return VerificationResult(*self._outcome(results), results, settle, window)

    # ----------------------------------------------------------------------------- outcome
    @staticmethod
    def _outcome(results: list[CriterionResult]) -> tuple[Outcome, str]:
        if not results:
            return Outcome.INCONCLUSIVE, "the action has no verification criteria"
        failed = [r for r in results if r.status == "fail"]
        unknown = [r for r in results if r.status == "unknown"]
        if failed:
            return Outcome.NOT_RESOLVED, "failed: " + "; ".join(r.statement for r in failed)
        if unknown:
            return Outcome.INCONCLUSIVE, "could not be established: " + "; ".join(r.statement for r in unknown)
        return Outcome.RESOLVED, f"all {len(results)} verification criteria held over the window"

    # ----------------------------------------------------------------------------- per-sample checks
    def _sample(self, caps, r: CriterionResult, applied_at: float) -> tuple[bool | None, str]:
        if r.check == "component_ready":
            return self._ready(caps, r.subject)
        if r.check == "dependency_available":
            host, _, port = r.subject.rpartition(":") if ":" in r.subject else (r.subject, "", "")
            h = caps.get_service_health(host, int(port) if port.isdigit() else None, "")
            if h is None:
                return None, f"{r.subject}: service state unavailable"
            need = int(r.expectation.get("ready_endpoints_min", 1))
            return h.ready_endpoints >= need, f"{r.subject}: {h.ready_endpoints} ready endpoint(s) (need {need})"
        if r.check == "entry_requests_succeed":
            if not self.entry:
                return None, "no synthetic request configured"
            res = caps.probe_request(*self.entry)
            if res is None:
                return None, "synthetic request could not be made"
            bound = int(r.expectation.get("status_below", 500))
            return 0 < res.status < bound, f"synthetic request to {self.entry[0]}: HTTP {res.status}"
        if r.check == "no_new_terminations":
            return self._new_terminations(caps, r, applied_at)
        return None, f"check '{r.check}' is not sampled"

    def _ready(self, caps, comp: str) -> tuple[bool | None, str]:
        now = self.clock()
        st = caps.get_resource_state(comp, TimeRange(now - 60, now))
        if st is None:
            return None, f"{comp}: state unavailable"
        not_ready = [i.name for i in st.instances if not i.ready]
        ok = st.desired > 0 and st.ready >= st.desired and not not_ready
        return ok, f"{comp}: {st.ready}/{st.desired} ready" + (f", not ready: {', '.join(not_ready[:3])}" if not_ready else "")

    def _new_terminations(self, caps, r: CriterionResult, applied_at: float) -> tuple[bool | None, str]:
        now = self.clock()
        st = caps.get_resource_state(r.subject, TimeRange(applied_at, now))
        if st is None:
            return None, f"{r.subject}: state unavailable"
        cause = r.expectation.get("cause")
        since = applied_at - START_SLACK_S
        new = {i.name for i in st.instances if (i.created or 0) >= since} | \
              {p.name for p in st.past_instances if (p.created or 0) >= since}
        terms = [(i.name, p.last_termination) for i in st.instances for p in i.processes if p.last_termination]
        terms += [(h.instance, h.termination) for h in st.history]
        hits = {(inst, t.finished_at) for inst, t in terms
                if inst in new and (cause is None or t.cause == cause) and (t.finished_at or 0) >= applied_at}
        limit = int(r.expectation.get("count", 0))
        return len(hits) <= limit, (f"{r.subject}: {len(hits)} termination(s)" + (f" ({cause})" if cause else "")
                                    + f" in {len(new)} instance(s) started after the change")

    # ----------------------------------------------------------------------------- window checks
    def _dependency_errors(self, caps, r: CriterionResult, start: float, end: float) -> tuple[bool | None, str]:
        st = caps.get_resource_state(r.subject, TimeRange(start, end))
        if st is None or not st.instances:
            return None, f"{r.subject}: no instances to read logs from"
        endpoint = r.expectation.get("endpoint")
        host = endpoint.rpartition(":")[0] if endpoint and ":" in endpoint else endpoint
        errors, read = 0, 0
        for inst in st.instances:
            for proc in inst.processes:
                lines = caps.get_logs(r.subject, inst.name, proc.name, TimeRange(start, end))
                if lines is None:
                    continue
                read += 1
                for rec in parse_records([(t, l) for t, l in lines if t is not None and t >= start]):
                    text = rec["msg"] if rec.get("_raw") else json.dumps({k: v for k, v in rec.items()
                                                                          if not k.startswith("_")})
                    if classify(text) not in DEPENDENCY_SIGNATURES:
                        continue
                    target, _ = extract_target(rec, text)
                    if host is None or (target or "").split(".")[0] == host.split(".")[0]:
                        errors += 1
        if not read:
            return None, f"{r.subject}: logs could not be read"
        limit = int(r.expectation.get("count", 0))
        return errors <= limit, (f"{r.subject}: {errors} dependency error line(s)" + (f" for {endpoint}" if endpoint
                                                                                        else "") + " during observation")

    def _error_ratio(self, caps, r: CriterionResult, start: float, end: float) -> tuple[bool | None, str]:
        series = caps.get_metrics("error_ratio", TimeRange(start, end), target=r.subject)
        if not series:
            return None, f"{r.subject}: no error-ratio metric available"
        lag = max(float(s.labels.get("window_s") or 0) for s in series)
        points = [(float(t), float(v)) for s in series for t, v in s.points if float(t) >= start + lag]
        if not points:
            return None, f"{r.subject}: no error-ratio points after the metric's {lag:.0f}s averaging lag"
        bound = float(r.expectation.get("ratio", 0.05))
        worst = max(v for _, v in points)
        return worst < bound, f"{r.subject}: highest error ratio {worst:.1%} over {len(points)} point(s) (bound {bound:.0%})"
