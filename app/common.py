"""Shared helpers for the demo services: structured JSON logging and cgroup memory info."""
import json
import os
import sys
from datetime import datetime, timezone

SERVICE = os.getenv("SERVICE_NAME", "app")
POD = os.getenv("POD_NAME", os.getenv("HOSTNAME", "local"))


def log(level: str, msg: str, **fields) -> None:
    """Emit one JSON log line to stdout (collected by the container runtime)."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "service": SERVICE,
        "pod": POD,
        "msg": msg,
    }
    rec.update(fields)
    sys.stdout.write(json.dumps(rec, default=str) + "\n")
    sys.stdout.flush()


def _read_int(path: str):
    try:
        with open(path) as f:
            raw = f.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def cgroup_memory():
    """Return (usage_bytes, limit_bytes) for this container, supporting cgroup v2 and v1."""
    usage = _read_int("/sys/fs/cgroup/memory.current")
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if usage is None:
        usage = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if limit is not None and limit > 1 << 60:
            limit = None
    return usage, limit


def rss_bytes():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None
