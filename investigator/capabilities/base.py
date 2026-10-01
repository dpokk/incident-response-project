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
    """One ended run of a process.

    `reason` and `exit_code` are the provider's own values (shown in reports); `cause` is the neutral
    classification the engine reasons with:
      memory_limit - killed for exceeding its memory limit
      error_exit   - the process exited on its own with an error
      killed       - killed by a signal from outside (e.g. a failed health check, a stop)
      completed    - exited successfully
    """
    reason: str | None
    exit_code: int | None
    started_at: float | None
    finished_at: float | None
    cause: str | None = None


@dataclass
class ProcessState:
    name: str
    state: str | None                  # running | waiting | terminated | None
    started_at: float | None = None
    waiting_reason: str | None = None      # provider's own word, e.g. "CrashLoopBackOff"
    waiting_message: str | None = None
    # Neutral classification of an abnormal wait (None = a normal wait such as starting up):
    #   restart_backoff | image_unavailable | invalid_configuration
    waiting_cause: str | None = None
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
    process_kind: str = "process"      # provider's word for a process, e.g. "container" (wording only)

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
    source: str                        # where this memory comes from, e.g. "kubernetes.history"
    instance_gone: bool = False        # the instance no longer exists
    logs_retained: bool = False        # the log lines of that run were retained (see get_log_history)
    generation: int | None = None      # which run of the process ended (0 = first run)
    memory_limit_bytes: float | None = None   # the process's memory limit per the component's definition


@dataclass
class PastInstance:
    """An instance of the component that existed during the time range but no longer exists."""
    name: str
    kind: str                          # provider's own word, e.g. "Pod"
    created: float | None
    gone_at: float | None              # when it was observed to disappear (None = not observed)
    retained_runs: int = 0             # runs (generations) whose log lines were retained


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
    past_instances: list[PastInstance] = field(default_factory=list)


# --------------------------------------------------------------------------- events, logs, config, changes

@dataclass
class EventRecord:
    component: str | None              # attributed by the provider; None for infrastructure events
    object_kind: str
    object_name: str
    type: str | None                   # e.g. Normal / Warning
    reason: str | None                 # provider's own word, e.g. "BackOff"
    message: str
    count: int
    first: float | None
    last: float | None
    # Neutral classification the engine reasons with (None = not significant for diagnosis):
    #   restart_backoff | health_check_failed | killed_by_health_check | stopped | scheduling_failed |
    #   scaled | instance_deleted | evicted | memory_limit | failed | node_problem
    category: str | None = None
    origin: str = "live"               # live (read from the system now) | retained (from evidence history)


# --------------------------------------------------------------------------- evidence history (Iteration 4)

@dataclass
class LogHistory:
    """Retained log lines of one run of a process that the live system can no longer serve (an earlier run,
    or a run of an instance that no longer exists)."""
    instance: str
    process: str
    generation: int                    # 0 = first run of the process in that instance
    lines: list                        # [(t, raw line)]
    instance_gone: bool
    dropped: int = 0                   # lines not retained (rate cap); their content is unknown
    termination: Termination | None = None   # how this run ended, if recorded


@dataclass
class ConfigChange:
    """A recorded change to configuration or to a component's definition."""
    component: str | None
    source: str                        # e.g. "configmap/backend-config", "Deployment/backend"
    item: str                          # what changed, e.g. a key or "image"
    before: str | None                 # None for sensitive items, which are never reported
    after: str | None
    sensitive: bool
    t: float | None                    # when the change happened, if the source says so
    t_earliest: float | None           # otherwise it happened after this observation ...
    t_latest: float | None             # ... and no later than this one


@dataclass
class Coverage:
    """A span of time for which a source retained evidence. Outside these spans nothing was recorded."""
    source: str                        # e.g. "kubernetes.history"
    start: float
    end: float
    detail: str = ""


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
    scope: str = ""                    # what this provider covers, e.g. a namespace

    # -- optional lifecycle hooks ---------------------------------------------------
    def reset(self) -> None:
        """Forget cached reads so the next calls see fresh state (used by pollers such as detection)."""

    def start_background_recording(self) -> None:
        """Begin recording state history the provider would otherwise forget (e.g. earlier crashes)."""

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

    # -- evidence history (Iteration 4); optional: a provider without history returns nothing -----------
    def get_log_history(self, component: str, time_range: TimeRange) -> list[LogHistory]:
        """Retained logs of runs the live system can no longer serve."""
        return []

    def get_configuration_history(self, component: str, time_range: TimeRange) -> list[ConfigChange]:
        """Recorded changes to the component's configuration and definition."""
        return []

    def get_evidence_coverage(self, time_range: TimeRange) -> list[Coverage]:
        """When evidence history was being recorded. Empty = no history for this range."""
        return []


class MetricsProvider(ABC):
    """Time series about the system (request rate, error ratio, memory)."""

    name: str = "metrics"

    @abstractmethod
    def get_metrics(self, metric: str, time_range: TimeRange, target: str | None = None,
                    component: str | None = None) -> list[MetricSeries]:
        """`metric` is a provider-independent name: request_rate, error_ratio, memory_working_set.
        `target`: the component whose requests are measured (request_rate, error_ratio).
        `component`: restrict per-instance series (memory_working_set) to one component's instances, including
        instances that no longer exist; each series is labelled with "instance" and "component".
        A series of rates/ratios carries label "window_s": each point averages over that many seconds before it,
        so a change shows up to that much later than it happened."""
