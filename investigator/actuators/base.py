"""The cluster-writing interface (Iteration 7). Deliberately narrow and typed.

Capabilities (capabilities/base.py) are read-only by contract; this is the separate, write-side interface. It has
exactly three operations - one per executable remediation type - and no generic escape hatch: no command, no YAML,
no arbitrary patch. Each operation is compare-and-set: it changes the value only if the live value still equals
`expected`, so a change made by someone else between the recheck and the write is never overwritten.

Every operation supports `dry_run=True` (validated by the provider, nothing persisted). Only the executor holds an
Actuator; providers.py (the composition root) is the one place that chooses a concrete one.
"""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field


@dataclass
class ChangeResult:
    accepted: bool                      # the provider accepted the operation (dry run: it would accept it)
    dry_run: bool
    operation: str                      # e.g. "set_memory_limit"
    target: str                         # e.g. "Deployment shop/backend container backend"
    before: str | None = None           # the live value read immediately before the write
    after: str | None = None            # the value the provider reports after the (dry-run) write
    detail: list[str] = field(default_factory=list)   # e.g. the individual provider operations performed
    error: str | None = None
    changed: bool = False               # something was persisted (True on success; on failure: a partial change)

    def to_dict(self) -> dict:
        return asdict(self)


class Actuator(ABC):
    name: str = "actuator"
    scope: str = ""                     # where it may write, e.g. a namespace

    @abstractmethod
    def set_memory_limit(self, component: str, process: str, expected_bytes: float, new_bytes: float,
                         dry_run: bool) -> ChangeResult:
        """Set one process's memory limit in a component's definition (the provider rolls the instances)."""

    @abstractmethod
    def set_replicas(self, component: str, expected: int, new: int, dry_run: bool) -> ChangeResult:
        """Set a component's desired replica count."""

    @abstractmethod
    def set_config_value(self, source: str, item: str, expected_value: str, new_value: str, restart_component: str,
                         dry_run: bool) -> ChangeResult:
        """Set one configuration item, then restart the component that reads it at start-up, so the value takes
        effect. One typed action; the provider performs (and dry-runs) both operations."""
