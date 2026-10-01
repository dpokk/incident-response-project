"""Generic log analysis: turns raw container logs into observable signatures.

Works on structured (JSON) and plain-text lines. Recognises common failure signatures that apply to
any application (DNS failures, refused/timed-out connections, auth errors, memory pressure, upstream
errors, Python tracebacks) and extracts the endpoint an error refers to when the message names one.
"""
import json
import re
from collections import defaultdict

# Ordered: the first matching signature wins (e.g. a DNS error message may also contain "failed").
SIGNATURES: list[tuple[str, re.Pattern]] = [(k, re.compile(p)) for k, p in [
    ("dns_resolution_failure", r"could not translate host name|Name or service not known|Temporary failure in name "
                               r"resolution|nodename nor servname|getaddrinfo failed|no such host|failed to resolve host"),
    ("connection_refused", r"[Cc]onnection refused|ECONNREFUSED"),
    ("connection_timeout", r"timeout expired|timed out|ETIMEDOUT|[Tt]imeout (?:error|exceeded)"),
    ("connection_reset", r"server closed the connection unexpectedly|[Cc]onnection reset|terminating connection due to "
                         r"administrator command|ServerDisconnected|EOF detected|connection is closed"),
    ("auth_failure", r"password authentication failed|authentication failed|[Aa]ccess denied|no password supplied"),
    ("missing_resource", r'(database|relation|role) "[^"]+" does not exist'),
    ("memory_pressure", r"MemoryError|[Oo]ut of memory|memory usage approaching|[Cc]annot allocate memory"),
    ("upstream_failure", r"upstream|[Bb]ad gateway|backend unavailable"),
]]
DEPENDENCY_SIGNATURES = {"dns_resolution_failure", "connection_refused", "connection_timeout", "connection_reset",
                         "auth_failure", "missing_resource"}

_TARGET_PATTERNS = [
    re.compile(r'at "(?P<host>[A-Za-z0-9.\-]+)" \([^)]*\), port (?P<port>\d+)'),
    re.compile(r'host(?: name)? "?(?P<host>[A-Za-z0-9.\-]+)"?(?:.{0,40}?port "?(?P<port>\d+))?'),
    re.compile(r"(?:https?|postgres(?:ql)?|mysql|redis|amqp)://(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9.\-]+)(?::(?P<port>\d+))?"),
    re.compile(r"\b(?P<host>[a-z][a-z0-9\-]*(?:\.[a-z0-9\-]+)*):(?P<port>\d{2,5})\b"),
]
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
_EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?:: (?P<message>.*))?$")
_RAW_LEVEL = re.compile(r"\b(ERROR|FATAL|PANIC|CRITICAL|WARNING|WARN)\b:?")
_NORMALIZE = [(re.compile(r"[0-9a-f]{8}-[0-9a-f-]{27,}"), "<uuid>"), (re.compile(r"\b\d+(\.\d+)?\b"), "<n>")]


def classify(text: str) -> str | None:
    for kind, rx in SIGNATURES:
        if rx.search(text):
            return kind
    return None


def extract_target(rec: dict, text: str) -> tuple[str | None, str | None]:
    host = rec.get("db_host") or rec.get("host") or rec.get("target_host")
    port = rec.get("db_port") or rec.get("port") or rec.get("target_port")
    if not host and rec.get("upstream"):
        m = _TARGET_PATTERNS[2].search(str(rec["upstream"]))
        if m:
            host, port = m.group("host"), m.group("port")
    if not host:
        for rx in _TARGET_PATTERNS:
            m = rx.search(text)
            if m and m.group("host") not in ("localhost",):
                host, port = m.group("host"), (m.groupdict().get("port") or port)
                break
    return (str(host) if host else None), (str(port) if port else None)


def normalize(msg: str) -> str:
    for rx, rep in _NORMALIZE:
        msg = rx.sub(rep, msg)
    return msg[:160]


def parse_records(lines: list[tuple[float | None, str]]) -> list[dict]:
    """Parse (timestamp, raw line) pairs into records; JSON lines keep their fields."""
    out = []
    for t, raw in lines:
        try:
            rec = json.loads(raw)
            if not isinstance(rec, dict):
                raise ValueError
            rec.setdefault("msg", "")
            rec["_raw"] = False
        except ValueError:
            m = _RAW_LEVEL.search(raw)
            level = None if not m else ("warning" if m.group(1).startswith("WARN") else "error")
            rec = {"msg": raw, "level": level, "_raw": True}
        rec["_t"] = t
        out.append(rec)
    return out


def find_tracebacks(records: list[dict]) -> list[dict]:
    """Python tracebacks printed as plain text (e.g. an unhandled exception that ended the process)."""
    found, cur = [], None
    for rec in records:
        if not rec["_raw"]:
            continue
        line = rec["msg"]
        if line.startswith("Traceback (most recent call last):"):
            cur = cur or {"frames": [], "t": rec["_t"], "chained": 0}
            continue
        if cur is None:
            continue
        m = _FRAME.match(line)
        if m:
            cur["frames"].append(m.groupdict())
            continue
        if line.startswith((" ", "\t")) or not line.strip():
            continue
        if line.startswith(("During handling of the above exception", "The above exception was the direct cause")):
            cur["chained"] += 1
            continue
        m = _EXC_LINE.match(line.strip())
        if m:
            app_frames = [f for f in cur["frames"] if "site-packages" not in f["file"] and "/lib/python" not in f["file"]]
            site = (app_frames or cur["frames"] or [None])[-1]
            found.append({"type": m.group("type"), "message": (m.group("message") or "")[:300], "t": cur["t"],
                          "frames": cur["frames"][-6:], "crash_site": site, "chained_exceptions": cur["chained"]})
        cur = None
    return found


def analyze(records: list[dict]) -> dict:
    """Summarise one container instance's records into signatures, error groups and exceptions."""
    sigs: dict[tuple, dict] = {}
    levels = defaultdict(int)
    errors: dict[str, dict] = {}
    for rec in records:
        level = (rec.get("level") or "").lower()
        text = " ".join(str(rec.get(k, "")) for k in ("msg", "error", "last_error", "sample_error", "reason") if rec.get(k))
        weight = rec["count"] if isinstance(rec.get("count"), int) and rec["count"] > 0 else 1
        if level:
            levels[level] += weight
        if rec["_raw"] and not level:
            continue  # other plain-text lines are only used by the traceback parser
        kind = classify(text) if level in ("error", "warning", "critical", "fatal") else None
        if kind:
            host, port = extract_target(rec, text)
            key = (kind, host, port)
            s = sigs.setdefault(key, {"signature": kind, "target_host": host, "target_port": port, "count": 0,
                                      "first": rec["_t"], "last": rec["_t"], "sample": text[:400], "level": level,
                                      "occurrences": []})
            s["count"] += weight
            s["occurrences"].append((rec["_t"], weight, text[:400]))
            s["first"] = min(filter(None, [s["first"], rec["_t"]]), default=None)
            s["last"] = max(filter(None, [s["last"], rec["_t"]]), default=None)
        if level in ("error", "critical", "fatal"):
            g = errors.setdefault(normalize(rec.get("msg", "")), {"message": rec.get("msg", "")[:200], "count": 0,
                                                                  "first": rec["_t"], "last": rec["_t"]})
            g["count"] += weight
            g["last"] = rec["_t"] or g["last"]
    return {"signatures": list(sigs.values()), "levels": dict(levels), "error_groups": list(errors.values()),
            "tracebacks": find_tracebacks(records), "lines": len(records),
            "first": records[0]["_t"] if records else None, "last": records[-1]["_t"] if records else None,
            "tail": [r.get("msg", "")[:300] for r in records[-5:]]}
