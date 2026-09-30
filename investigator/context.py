"""Incident context: what the investigation knows before it starts. Provider-independent.

Built from the detection signals only. It carries no failure category and no scenario name, just the
symptoms, when they were seen, which components they point at, and the time window to examine.
"""
from dataclasses import dataclass, field

from .capabilities import TimeRange


@dataclass
class IncidentContext:
    id: str
    window: TimeRange
    detected_at: float | None
    signals: list[dict] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)   # components named by the signals, in signal order
    entry: tuple | None = None                          # (service, port, path) of the user-facing entry point
    metrics_target: str | None = None                   # component whose request metrics describe user impact

    @classmethod
    def from_incident(cls, incident: dict, start: float, end: float, entry: tuple | None = None,
                      metrics_target: str | None = None) -> "IncidentContext":
        suspects = []
        for s in incident.get("signals", []):
            kind, _, name = (s.get("subject") or "").partition("/")
            if kind in ("component", "workload") and name and name not in suspects:  # "workload/": older records
                suspects.append(name)
        return cls(id=incident["id"], window=TimeRange(start, end), detected_at=incident.get("detected_at"),
                   signals=list(incident.get("signals", [])), suspects=suspects, entry=entry,
                   metrics_target=metrics_target)

    @property
    def incident(self) -> dict:
        """The plain incident record used by reports."""
        return {"id": self.id, "detected_at": self.detected_at, "signals": self.signals}
