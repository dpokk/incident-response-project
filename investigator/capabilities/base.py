"""Provider-independent capability interface (Iteration 3).

The investigation engine asks for evidence through these capabilities; it never talks to Kubernetes,
a cloud API or a metrics backend directly. Each provider implements the interface in an adapter
(`kubernetes.py` is the first; `prometheus.py` supplies metrics).

Vocabulary (neutral on purpose):
  component - a deployable unit of the system (Kubernetes: a Deployment/StatefulSet)
  instance  - one running copy of a component (Kubernetes: a Pod)
  process   - a program inside an instance (Kubernetes: a container)

Records are plain data. Provider-specific names that users recognise (e.g. "Deployment", "Pod",
"OOMKilled") travel as *values* in the records (`kind`, `reason`) so reports stay familiar, but no
investigation code depends on a provider's API or object model.

Capabilities are read-only by contract. Nothing here changes the observed system.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TimeRange:
    start: float
    end: float


# --------------------------------------------------------------------------- resource state

@dataclass
class Termination:
    """One ended run of a process."""
    reason: str | None
    exit_code: int | None
    started_at: float | None
    finished_at: float | None


@dataclass
class ProcessState:
    name: str
    state: str | None                  # running | waiting | terminated | None
    started_at: float | None = None
    waiting_reason: str | None = None
    waiting_message: str | None = None
    restarts: int = 0
    last_termination: Termination | None = None
    memory_limit_bytes: float | None = None
    cpu_limit_cores: float | None = None


@dataclass
class InstanceState:
    name: str
    kind: str                          # provider's own word, e.g. "Pod"
    phase: str | None
    ready: bool
    ready_since: float | None
    unschedulable: bool
    created: float | None
    processes: list[ProcessState] = field(default_factory=list)

    @property
    def restarts(self) -> int:
        return sum(p.restarts for p in self.processes)


@dataclass
class TerminationRecord:
    """A termination remembered by the provider beyond what current state shows (e.g. an earlier crash)."""
    instance: str
    process: str | None
    termination: Termination
    restarts: int | None
    source: str                        # where this memory comes from, e.g. "kubernetes.pod_journal"


@dataclass
class ResourceState:
    component: str
    kind: str                          # provider's own word, e.g. "Deployment"
    scope: str                         # where it lives, e.g. a namespace
    desired: int
    ready: int
    available: int
    limits: dict                       # process name -> {"cpu": ..., "memory": ...}
    instances: list[InstanceState] = field(default_factory=list)
    history: list[TerminationRecord] = field(default_factory=list)


# --------------------------------------------------------------------------- events, logs, config, changes

@dataclass
class EventRecord:
    component: str | None              # attributed by the provider; None for infrastructure events
    object_kind: str
    object_name: str
    type: str | None                   # e.g. Normal / Warning
    reason: str | None
    message: str
    count: int
    first: float | None
    last: float | None


@dataclass
class ConfigEntry:
    process: str
    name: str
    value: str | None                  # sensitive values are available for parsing, never for reporting
    sensitive: bool
    source: str                        # e.g. "configmap/backend-config", "secret/postgres-credentials", "literal"
    modified: float | None


@dataclass
class DependencyRef:
    """An endpoint a component is configured to call."""
    host: str
    port: int | None
    type: str                          # PostgreSQL, HTTP service, ...
    variable: str
    process: str
    source: str
    sensitive_source: bool
    source_modified: float | None


@dataclass
class Change:
    component: str
    component_kind: str                # provider's word, e.g. "Deployment"
    kind: str                          # e.g. "rollout"
    at: float | None
    revision: str | None
    detail: str                        # provider's identifier for the change, e.g. a ReplicaSet name


# --------------------------------------------------------------------------- dependency health

@dataclass
class BackingComponent:
    component: str
    kind: str
    desired: int
    ready: int


@dataclass
class SimilarService:
    name: str
    ports: list
    ready: int
    name_similarity: float
    port_match: bool


@dataclass
class ServiceHealth:
    """What serves a configured endpoint."""
    host: str
    port: int | None
    internal: bool                     # resolvable inside the environment (vs an external endpoint)
    exists: bool | None
    name: str
    scope: str | None
    kind: str = "service"              # provider's words, used only for wording reports
    scope_kind: str = "scope"
    address_kind: str = "address"
    instance_kind: str = "instance"
    selector: dict = field(default_factory=dict)
    address: str | None = None
    ports: list = field(default_factory=list)
    other_scopes: list = field(default_factory=list)   # same name found elsewhere
    ready_endpoints: int = 0
    not_ready_endpoints: int = 0
    endpoint_instances: list = field(default_factory=list)
    backing: list[BackingComponent] = field(default_factory=list)
    similar: list[SimilarService] = field(default_factory=list)


@dataclass
class ConnectivityResult:
    from_instance: str | None          # None = no running instance to test from
    host: str
    port: int
    dns: str | None = None             # ok | error
    tcp: str | None = None             # ok | refused | timeout | error
    addresses: list = field(default_factory=list)
    error: str | None = None
    ms: int | None = None
    skipped: str | None = None         # reason the check could not run


@dataclass
class RequestResult:
    status: int
    body: str


@dataclass
class MetricSeries:
    labels: dict
    points: list                       # [(t, value)]


# --------------------------------------------------------------------------- interfaces

class ResourceProvider(ABC):
    """Everything about the running system except metrics."""

    name: str = "provider"

    @abstractmethod
    def list_components(self) -> list[str]: ...

    @abstractmethod
    def get_resource_state(self, component: str, time_range: TimeRange) -> ResourceState | None: ...

    @abstractmethod
    def get_events(self, time_range: TimeRange, infrastructure: bool = False) -> list[EventRecord]: ...

    @abstractmethod
    def get_logs(self, component: str, instance: str, process: str, time_range: TimeRange,
                 previous: bool = False) -> list[tuple[float | None, str]]: ...

    @abstractmethod
    def get_configuration(self, component: str) -> list[ConfigEntry]: ...

    @abstractmethod
    def get_dependencies(self, component: str) -> list[DependencyRef]: ...

    @abstractmethod
    def list_services(self) -> list[ServiceHealth]:
        """Every service in the investigated scope, with its endpoint counts."""

    @abstractmethod
    def get_service_health(self, host: str, port: int | None, dep_type: str) -> ServiceHealth: ...

    @abstractmethod
    def check_connectivity(self, from_component: str, host: str, port: int) -> ConnectivityResult: ...

    @abstractmethod
    def probe_request(self, service: str, port: str, path: str) -> RequestResult: ...

    @abstractmethod
    def get_deployment_history(self, time_range: TimeRange) -> list[Change]: ...


class MetricsProvider(ABC):
    """Time series about the system (request rate, error ratio, memory)."""

    name: str = "metrics"

    @abstractmethod
    def get_metrics(self, metric: str, time_range: TimeRange, target: str | None = None) -> list[MetricSeries]:
        """`metric` is a provider-independent name: request_rate, error_ratio, memory_working_set."""
