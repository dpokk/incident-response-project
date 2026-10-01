"""Settings loaded from environment / .env file."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # What we watch: every workload in this namespace. The investigator is never told which one broke.
    namespace: str = field(default_factory=lambda: _env("TARGET_NAMESPACE", "shop"))
    kube_context: str = field(default_factory=lambda: _env("KUBE_CONTEXT", ""))

    # User-facing entry point, probed synthetically through the API server's service proxy
    entry_service: str = field(default_factory=lambda: _env("ENTRY_SERVICE", "frontend"))
    entry_port: str = field(default_factory=lambda: _env("ENTRY_PORT", "8080"))
    entry_path: str = field(default_factory=lambda: _env("ENTRY_PATH", "/api/orders?quantity=1&total=10"))

    # Optional metrics source (Iteration 1 Prometheus). The investigation works without it.
    prometheus_enabled: bool = field(default_factory=lambda: _bool("PROMETHEUS_ENABLED", True))
    prometheus_url: str = field(default_factory=lambda: _env("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    auto_port_forward: bool = field(default_factory=lambda: _bool("PROMETHEUS_AUTO_PORT_FORWARD", True))
    prometheus_namespace: str = field(default_factory=lambda: _env("PROMETHEUS_NAMESPACE", "monitoring"))
    prometheus_service: str = field(default_factory=lambda: _env("PROMETHEUS_SERVICE", "prometheus"))
    entry_app: str = field(default_factory=lambda: _env("ENTRY_APP", "frontend"))

    # Active dependency probes: a fixed, read-only DNS + TCP check run inside a consumer pod
    active_probes: bool = field(default_factory=lambda: _bool("ALLOW_ACTIVE_PROBES", True))
    # "planned": the investigation planner follows the evidence (default, Iteration 3)
    # "exhaustive": the fixed Iteration 2 procedure over every component (kept for comparison)
    investigation_strategy: str = field(default_factory=lambda: _env("INVESTIGATION_STRATEGY", "planned").lower())

    # Detection
    poll_interval_s: float = field(default_factory=lambda: _float("POLL_INTERVAL_S", 5))
    probe_failure_threshold: float = field(default_factory=lambda: _float("PROBE_FAILURE_THRESHOLD", 0.5))
    error_log_threshold: float = field(default_factory=lambda: _float("ERROR_LOG_THRESHOLD", 5))
    error_rate_threshold: float = field(default_factory=lambda: _float("ERROR_RATE_THRESHOLD", 0.05))
    not_ready_grace_s: float = field(default_factory=lambda: _float("NOT_READY_GRACE_S", 20))
    investigate_delay_s: float = field(default_factory=lambda: _float("INVESTIGATE_DELAY_S", 40))
    resolve_stable_s: float = field(default_factory=lambda: _float("RESOLVE_STABLE_S", 45))
    lookback_s: float = field(default_factory=lambda: _float("LOOKBACK_S", 300))

    # Slack delivery of the report (optional)
    slack_bot_token: str = field(default_factory=lambda: _env("SLACK_BOT_TOKEN"))
    slack_channel: str = field(default_factory=lambda: _env("SLACK_CHANNEL"))
    slack_webhook_url: str = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))
    slack_post_detection: bool = field(default_factory=lambda: _bool("SLACK_POST_DETECTION", True))
    # Human review (Iteration 6): Socket Mode app-level token (xapp-...) for inbound Approve/Reject interactions,
    # and the Slack user IDs allowed to make a decision. Never logged.
    slack_app_token: str = field(default_factory=lambda: _env("SLACK_APP_TOKEN"))
    slack_approvers: frozenset = field(default_factory=lambda: frozenset(
        a.strip() for a in _env("SLACK_APPROVERS").split(",") if a.strip()))

    # Evidence history (Iteration 4): a recorder retains what the platform forgets (earlier runs' logs, deleted
    # instances, expired events, earlier configuration) in a local SQLite file under state/.
    history_enabled: bool = field(default_factory=lambda: _bool("HISTORY_ENABLED", True))
    history_retention_h: float = field(default_factory=lambda: _float("HISTORY_RETENTION_H", 24))
    history_max_lines_per_min: int = field(default_factory=lambda: int(_float("HISTORY_MAX_LINES_PER_MIN", 3000)))

    state_dir: Path = ROOT / "state"
    reports_dir: Path = ROOT / "reports"

    @property
    def history_path(self) -> Path:
        return self.state_dir / "history.db"

    @property
    def reviews_path(self) -> Path:
        return self.state_dir / "reviews.db"

    @property
    def slack_threads_path(self) -> Path:
        return self.state_dir / "slack_threads.json"

    @property
    def slack_configured(self) -> bool:
        return bool((self.slack_bot_token and self.slack_channel) or self.slack_webhook_url)
