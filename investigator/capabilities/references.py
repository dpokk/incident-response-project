"""Provider-independent parsing of configuration into dependency references.

Any provider whose components are configured through key/value settings (environment variables,
parameter stores, ...) can use this to implement `get_dependencies`.
"""
import re

from .base import ConfigEntry, DependencyRef

_URL = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9.\-]+)(?::(?P<port>\d+))?")
SCHEME_TYPES = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL", "mysql": "MySQL", "redis": "Redis",
                "mongodb": "MongoDB", "amqp": "RabbitMQ", "http": "HTTP service", "https": "HTTP service",
                "kafka": "Kafka"}
PORT_TYPES = {5432: "PostgreSQL", 3306: "MySQL", 6379: "Redis", 27017: "MongoDB", 5672: "RabbitMQ", 9092: "Kafka"}
SCHEME_PORTS = {"postgres": 5432, "postgresql": 5432, "mysql": 3306, "redis": 6379, "mongodb": 27017,
                "amqp": 5672, "http": 80, "https": 443}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def extract_references(config: list[ConfigEntry]) -> list[DependencyRef]:
    """Endpoints a component is configured to talk to (URL values and *_HOST/*_PORT pairs)."""
    refs, by_name = [], {(e.process, e.name): e for e in config}
    for e in config:
        val = e.value
        if not isinstance(val, str):
            continue
        m = _URL.match(val.strip())
        if m and m.group("host") not in LOCAL_HOSTS:
            scheme = m.group("scheme").lower()
            port = int(m.group("port") or SCHEME_PORTS.get(scheme, 0)) or None
            refs.append(_ref(e, m.group("host"), port, SCHEME_TYPES.get(scheme) or PORT_TYPES.get(port or 0, "service")))
        elif e.name.upper().endswith("_HOST") and val and val not in LOCAL_HOSTS and re.match(r"^[A-Za-z0-9.\-]+$", val):
            port_entry = by_name.get((e.process, e.name[:-5] + "_PORT"))
            port = int(port_entry.value) if port_entry and str(port_entry.value or "").isdigit() else None
            refs.append(_ref(e, val, port, PORT_TYPES.get(port or 0, "service")))
    uniq = {}
    for r in refs:
        uniq.setdefault((r.host, r.port), r)
    return list(uniq.values())


def _ref(entry: ConfigEntry, host: str, port: int | None, dep_type: str) -> DependencyRef:
    return DependencyRef(host=host, port=port, type=dep_type, variable=entry.name, process=entry.process,
                         source=entry.source, sensitive_source=entry.sensitive, source_modified=entry.modified)
