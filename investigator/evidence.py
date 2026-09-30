"""Evidence model: facts are observations only. Interpretation happens later, in diagnosis.py.

A Fact records *what was observed*, *where* (subject), *by which source*, and *when*. It never
states a conclusion ("PostgreSQL is down"); it states the observation ("Service shop/postgres has
0 ready endpoints"). Diagnoses cite fact IDs so every conclusion is traceable to observations.
"""
from dataclasses import asdict, dataclass, field


@dataclass
class Fact:
    id: str
    source: str          # e.g. kubernetes.pod_status, kubernetes.events, logs, configuration, dependency_probe
    subject: str         # e.g. workload/backend, pod/backend-abc, service/postgres, dependency/postgres:5432
    kind: str            # machine-readable observation type, e.g. container_terminated, log_signature
    text: str            # human-readable observation (no interpretation)
    data: dict = field(default_factory=dict)
    t: float | None = None  # when the observed thing happened, if known

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceStore:
    def __init__(self):
        self.facts: list[Fact] = []
        self.trace: list[dict] = []   # investigation steps / capability calls, in order

    def add(self, source: str, subject: str, kind: str, text: str, t: float | None = None, **data) -> Fact:
        fact = Fact(id=f"F{len(self.facts) + 1}", source=source, subject=subject, kind=kind, text=text, data=data, t=t)
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
        store.facts = [Fact(**f) for f in d["facts"]]
        store.trace = d.get("trace", [])
        return store
