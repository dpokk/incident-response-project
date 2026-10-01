"""Evidence model: facts are observations only. Interpretation happens later, in diagnosis.py.

A Fact records *what was observed*, *where* (subject), *by which source*, and *when*. It never
states a conclusion ("PostgreSQL is down"); it states the observation ("Service shop/postgres has
0 ready endpoints"). Diagnoses cite fact IDs so every conclusion is traceable to observations.

Time (Iteration 4) keeps three moments apart and never manufactures precision:
  event time       `t` - when the observed thing happened, read with `t_basis`
  observation time `observed_at` - when the source saw it (e.g. the recorder, or a live read)
  collection time  `collected_at` - when this investigation collected it
`origin` says whether the evidence was read live or retained by the evidence history.
"""
import time
from dataclasses import asdict, dataclass, field

# How to read Fact.t:
T_BASES = {
    "exact": "the source stated when it happened",
    "bounded": "it happened between t_earliest and t_latest; t is the latest possible time",
    "observed": "only the time it was observed is known; it happened at or before t",
    "before_window": "it began before the evidence window; t is when it was last observed",
    "unknown": "no time is known",
}


@dataclass
class Fact:
    id: str
    source: str          # e.g. kubernetes.pod_status, kubernetes.events, logs, configuration, dependency_probe
    subject: str         # e.g. workload/backend, pod/backend-abc, service/postgres, dependency/postgres:5432
    kind: str            # machine-readable observation type, e.g. container_terminated, log_signature
    text: str            # human-readable observation (no interpretation)
    data: dict = field(default_factory=dict)
    t: float | None = None  # when the observed thing happened, if known
    # How to read `t` (see T_BASES). For "before_window" events, data["first_seen"] keeps the source's own
    # first timestamp and data["observed_at"] the last observation.
    t_basis: str = "exact"
    t_earliest: float | None = None   # bounds, when t_basis is "bounded"
    t_latest: float | None = None
    origin: str = "live"              # live | retained | mixed (live and retained evidence combined)
    collected_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceStore:
    def __init__(self, clock=time.time):
        self.facts: list[Fact] = []
        self.trace: list[dict] = []   # investigation steps / capability calls, in order
        self.clock = clock

    def add(self, source: str, subject: str, kind: str, text: str, t: float | None = None,
            t_basis: str = "exact", t_earliest: float | None = None, t_latest: float | None = None,
            origin: str = "live", **data) -> Fact:
        if t_basis not in T_BASES:
            raise ValueError(f"unknown t_basis {t_basis!r}")
        if t is None and t_basis == "exact":
            t_basis = "unknown"
        fact = Fact(id=f"F{len(self.facts) + 1}", source=source, subject=subject, kind=kind, text=text, data=data,
                    t=t, t_basis=t_basis, t_earliest=t_earliest, t_latest=t_latest, origin=origin,
                    collected_at=self.clock())
        self.facts.append(fact)
        return fact

    def find(self, kind: str | None = None, subject: str | None = None, source: str | None = None,
             **match) -> list[Fact]:
        out = []
        for f in self.facts:
            if kind and f.kind != kind:
                continue
            if subject and f.subject != subject:
                continue
            if source and f.source != source:
                continue
            if any(f.data.get(k) != v for k, v in match.items()):
                continue
            out.append(f)
        return out

    def by_id(self, fid: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fid), None)

    def step(self, name: str, detail: str = "", **info) -> None:
        self.trace.append({"step": name, "detail": detail, **info})

    def to_dict(self) -> dict:
        return {"facts": [f.to_dict() for f in self.facts], "trace": self.trace}

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceStore":
        store = cls()
        store.facts = [Fact(**f) for f in d["facts"]]   # facts saved before Iteration 4 take the field defaults
        store.trace = d.get("trace", [])
        return store
