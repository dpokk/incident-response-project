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
    # What we watch
    namespace: str = field(default_factory=lambda: _env("TARGET_NAMESPACE", "shop"))
    entry_app: str = field(default_factory=lambda: _env("ENTRY_APP", "frontend"))  # user-facing edge service
    kube_context: str = field(default_factory=lambda: _env("KUBE_CONTEXT", ""))

    # Metrics
    prometheus_url: str = field(default_factory=lambda: _env("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    auto_port_forward: bool = field(default_factory=lambda: _bool("PROMETHEUS_AUTO_PORT_FORWARD", True))
    prometheus_namespace: str = field(default_factory=lambda: _env("PROMETHEUS_NAMESPACE", "monitoring"))
    prometheus_service: str = field(default_factory=lambda: _env("PROMETHEUS_SERVICE", "prometheus"))

    # Detection
    poll_interval_s: float = field(default_factory=lambda: _float("POLL_INTERVAL_S", 5))
    error_rate_threshold: float = field(default_factory=lambda: _float("ERROR_RATE_THRESHOLD", 0.05))
    cpu_threshold: float = field(default_factory=lambda: _float("CPU_THRESHOLD", 0.9))
    memory_threshold: float = field(default_factory=lambda: _float("MEMORY_THRESHOLD", 0.85))
    resolve_stable_s: float = field(default_factory=lambda: _float("RESOLVE_STABLE_S", 45))
    max_incident_wait_s: float = field(default_factory=lambda: _float("MAX_INCIDENT_WAIT_S", 600))
    lookback_s: float = field(default_factory=lambda: _float("LOOKBACK_S", 300))

    # LLM: "nvidia" (OpenAI-compatible NIM endpoint from build.nvidia.com) or "anthropic"
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "nvidia").lower())
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
    nvidia_api_key: str = field(default_factory=lambda: _env("NVIDIA_API_KEY"))
    nvidia_base_url: str = field(default_factory=lambda: _env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"))
    nvidia_model: str = field(default_factory=lambda: _env("NVIDIA_MODEL", "deepseek-ai/deepseek-v4.1-flash"))
    # Tried in order if the primary model times out / errors (free hosted endpoints can be overloaded)
    nvidia_fallback_models: tuple = field(default_factory=lambda: tuple(
        m.strip() for m in _env("NVIDIA_FALLBACK_MODELS", "nvidia/nemotron-3-super-120b-a12b").split(",") if m.strip()))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 120))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-opus-5-5"))

    @property
    def llm_model(self) -> str:
        return self.nvidia_model if self.llm_provider == "nvidia" else self.anthropic_model

    @property
    def llm_configured(self) -> bool:
        if not self.llm_enabled:
            return False
        if self.llm_provider == "nvidia":
            return bool(self.nvidia_api_key)
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    # Slack
    slack_bot_token: str = field(default_factory=lambda: _env("SLACK_BOT_TOKEN"))
    slack_channel: str = field(default_factory=lambda: _env("SLACK_CHANNEL"))
    slack_webhook_url: str = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))
    slack_post_detection: bool = field(default_factory=lambda: _bool("SLACK_POST_DETECTION", True))

    state_dir: Path = ROOT / "state"
    reports_dir: Path = ROOT / "reports"

    @property
    def slack_configured(self) -> bool:
        return bool((self.slack_bot_token and self.slack_channel) or self.slack_webhook_url)
