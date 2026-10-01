"""Evidence history store (Iteration 4): evidence that outlives the objects it describes.

A provider-side recorder writes what it observes here while the system runs; the provider's adapter reads
it back to answer capability calls after the original instances are gone. The investigation engine never
uses this module directly: it only sees retained evidence through the capability layer.

One local SQLite file (stdlib, WAL mode so a recorder can write while an investigation reads). The schema is
provider-neutral (component / instance / process / generation), like capabilities/base.py.

Honesty rules built into the store:
  * nothing is invented: rows hold what was observed, with when it happened (`t`, if the source said) and
    when it was observed (`observed_at`);
  * coverage is explicit: recorder sessions say which time spans were being recorded at all;
  * loss is explicit: log lines dropped by the rate cap are counted per minute in `log_gaps`;
  * sensitive values are never stored, only fingerprints (see `fingerprint`).
"""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, provider TEXT, scope TEXT, started_at REAL, last_seen_at REAL);
CREATE TABLE IF NOT EXISTS instances (
    scope TEXT, instance TEXT, component TEXT, kind TEXT, created REAL, first_seen REAL, last_seen REAL,
    gone_at REAL, PRIMARY KEY (scope, instance));
CREATE TABLE IF NOT EXISTS lifecycle (
    scope TEXT, component TEXT, instance TEXT, process TEXT, kind TEXT, t REAL, t_basis TEXT, observed_at REAL,
    data TEXT, UNIQUE (scope, instance, process, kind, t));
CREATE INDEX IF NOT EXISTS lifecycle_by_component ON lifecycle (scope, component, t);
CREATE TABLE IF NOT EXISTS log_lines (
    scope TEXT, component TEXT, instance TEXT, process TEXT, generation INTEGER, t REAL, observed_at REAL,
    line TEXT, UNIQUE (scope, instance, process, generation, t, line));
CREATE INDEX IF NOT EXISTS logs_by_component ON log_lines (scope, component, t);
CREATE TABLE IF NOT EXISTS log_gaps (
    scope TEXT, component TEXT, instance TEXT, process TEXT, generation INTEGER, minute REAL, dropped INTEGER,
    PRIMARY KEY (scope, instance, process, generation, minute));
CREATE TABLE IF NOT EXISTS events (
    scope TEXT, component TEXT, object_kind TEXT, object_name TEXT, type TEXT, reason TEXT, message TEXT,
    count INTEGER, first REAL, last REAL, first_observed REAL, observed_at REAL,
    PRIMARY KEY (scope, object_name, reason, first, message));
CREATE TABLE IF NOT EXISTS object_versions (
    scope TEXT, kind TEXT, name TEXT, component TEXT, observed_at REAL, checked_at REAL,
    previous_checked_at REAL, modified_at REAL, fingerprint TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS versions_by_name ON object_versions (scope, kind, name, observed_at);
"""


def fingerprint(value) -> str:
    """Stable short digest: lets the store notice that a sensitive value changed without keeping the value."""
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


class HistoryStore:
    def __init__(self, path: Path, clock=time.time, max_lines_per_minute: int = 3000):
        self.path, self.clock, self.max_lines = Path(path), clock, max_lines_per_minute
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._minute_counts: dict[tuple, int] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, rows: list | tuple, many: bool = False) -> int:
        with self._lock:
            cur = self._db.executemany(sql, rows) if many else self._db.execute(sql, rows)
            self._db.commit()
            return cur.rowcount

    def _read(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ recorder sessions (coverage)
    def open_session(self, provider: str, scope: str) -> int:
        now = self.clock()
        with self._lock:
            cur = self._db.execute("INSERT INTO sessions (provider, scope, started_at, last_seen_at) VALUES (?,?,?,?)",
                                   (provider, scope, now, now))
            self._db.commit()
            return cur.lastrowid

    def heartbeat(self, session_id: int) -> None:
        self._write("UPDATE sessions SET last_seen_at=? WHERE id=?", (self.clock(), session_id))

    def sessions(self, scope: str, start: float, end: float) -> list[dict]:
        return [dict(r) for r in self._read(
            "SELECT * FROM sessions WHERE scope=? AND last_seen_at>=? AND started_at<=? ORDER BY started_at",
            (scope, start, end))]

    # ------------------------------------------------------------------ instances and lifecycle
    def upsert_instance(self, scope: str, instance: str, component: str | None, kind: str, created: float | None) -> None:
        now = self.clock()
        self._write("INSERT INTO instances (scope, instance, component, kind, created, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT (scope, instance) DO UPDATE SET last_seen=excluded.last_seen, "
                    "component=COALESCE(excluded.component, instances.component)",
                    (scope, instance, component, kind, created, now, now))

    def mark_gone(self, scope: str, instance: str, t: float) -> None:
        self._write("UPDATE instances SET gone_at=COALESCE(gone_at, ?) WHERE scope=? AND instance=?", (t, scope, instance))

    def instances(self, scope: str, component: str, start: float, end: float) -> list[dict]:
        """Instances of a component that existed at some point in [start, end]."""
        return [dict(r) for r in self._read(
            "SELECT * FROM instances WHERE scope=? AND component=? AND COALESCE(created, first_seen)<=? "
            "AND COALESCE(gone_at, last_seen)>=? ORDER BY COALESCE(created, first_seen)", (scope, component, end, start))]

    def component_of(self, scope: str, instance: str) -> str | None:
        row = self._read("SELECT component FROM instances WHERE scope=? AND instance=?", (scope, instance))
        return row[0]["component"] if row else None

    def add_lifecycle(self, scope: str, component: str | None, instance: str, process: str | None, kind: str,
                      t: float | None, t_basis: str = "exact", **data) -> None:
        self._write("INSERT OR IGNORE INTO lifecycle VALUES (?,?,?,?,?,?,?,?,?)",
                    (scope, component, instance, process, kind, t, t_basis, self.clock(), json.dumps(data, default=str)))

    def lifecycle(self, scope: str, component: str, start: float, end: float, kind: str | None = None) -> list[dict]:
        rows = self._read("SELECT * FROM lifecycle WHERE scope=? AND component=? AND COALESCE(t, observed_at) BETWEEN ? AND ?"
                          + (" AND kind=?" if kind else "") + " ORDER BY COALESCE(t, observed_at)",
                          (scope, component, start, end) + ((kind,) if kind else ()))
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    # ------------------------------------------------------------------ logs
    def add_log_lines(self, scope: str, component: str | None, instance: str, process: str, generation: int,
                      lines: list[tuple[float | None, str]]) -> int:
        """Store lines of one run (generation) of a process. Lines beyond the per-minute cap are counted as
        dropped instead of stored, so the loss shows up as a gap rather than silently missing evidence."""
        now = self.clock()
        keep, dropped = [], {}
        for t, line in lines:
            minute = float(int((t or now) // 60) * 60)
            k = (instance, process, generation, minute)
            n = self._minute_counts.get(k, 0)
            if n >= self.max_lines:
                dropped[minute] = dropped.get(minute, 0) + 1
                continue
            self._minute_counts[k] = n + 1
            keep.append((scope, component, instance, process, generation, t, now, line))
        if len(self._minute_counts) > 20000:      # counters are only needed for recent minutes
            cutoff = (now // 60 - 10) * 60
            self._minute_counts = {k: v for k, v in self._minute_counts.items() if k[3] >= cutoff}
        n = self._write("INSERT OR IGNORE INTO log_lines VALUES (?,?,?,?,?,?,?,?)", keep, many=True) if keep else 0
        for minute, count in dropped.items():
            self._write("INSERT INTO log_gaps VALUES (?,?,?,?,?,?,?) ON CONFLICT (scope, instance, process, generation, "
                        "minute) DO UPDATE SET dropped=dropped+excluded.dropped",
                        (scope, component, instance, process, generation, minute, count))
        return n

    def log_generations(self, scope: str, component: str, start: float, end: float) -> list[dict]:
        """Runs of processes of `component` with retained log lines in [start, end]."""
        return [dict(r) for r in self._read(
            "SELECT instance, process, generation, MIN(t) AS first_t, MAX(t) AS last_t, COUNT(*) AS lines "
            "FROM log_lines WHERE scope=? AND component=? AND t BETWEEN ? AND ? "
            "GROUP BY instance, process, generation ORDER BY first_t", (scope, component, start, end))]

    def log_lines(self, scope: str, instance: str, process: str, generation: int, start: float, end: float) -> list[tuple]:
        return [(r["t"], r["line"]) for r in self._read(
            "SELECT t, line FROM log_lines WHERE scope=? AND instance=? AND process=? AND generation=? AND t BETWEEN ? AND ? "
            "ORDER BY t", (scope, instance, process, generation, start, end))]

    def log_gaps(self, scope: str, instance: str, process: str, generation: int, start: float, end: float) -> list[dict]:
        return [dict(r) for r in self._read(
            "SELECT * FROM log_gaps WHERE scope=? AND instance=? AND process=? AND generation=? AND minute BETWEEN ? AND ? "
            "ORDER BY minute", (scope, instance, process, generation, start - 60, end))]

    def object_names(self, scope: str, kind: str) -> list[str]:
        return [r["name"] for r in self._read("SELECT DISTINCT name FROM object_versions WHERE scope=? AND kind=?",
                                              (scope, kind))]

    # ------------------------------------------------------------------ events
    def upsert_event(self, scope: str, component: str | None, object_kind: str, object_name: str, type_: str | None,
                     reason: str | None, message: str, count: int, first: float | None, last: float | None) -> None:
        now = self.clock()
        self._write("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (scope, object_name, reason, first, "
                    "message) DO UPDATE SET count=MAX(count, excluded.count), last=MAX(COALESCE(last, 0), "
                    "COALESCE(excluded.last, 0)), observed_at=excluded.observed_at, "
                    "component=COALESCE(events.component, excluded.component)",
                    (scope, component, object_kind, object_name, type_, reason, message, count, first, last, now, now))

    def events(self, scope: str, start: float, end: float) -> list[dict]:
        return [dict(r) for r in self._read(
            "SELECT * FROM events WHERE scope=? AND COALESCE(last, first, observed_at)>=? AND COALESCE(first, last, "
            "observed_at)<=? ORDER BY first", (scope, start, end))]

    # ------------------------------------------------------------------ configuration / deployment versions
    def add_version(self, scope: str, kind: str, name: str, component: str | None, content: dict,
                    modified_at: float | None = None) -> bool:
        """Record an observation of an object. A new row is written only when its content changed; otherwise the
        latest row's `checked_at` moves forward. A change therefore happened after the previous row's
        `checked_at` and no later than the new row's `observed_at`: that interval is all the recorder knows
        unless the source states `modified_at` itself."""
        fp, now = fingerprint(content), self.clock()
        last = self._read("SELECT rowid, fingerprint, checked_at FROM object_versions WHERE scope=? AND kind=? AND name=? "
                          "ORDER BY observed_at DESC, rowid DESC LIMIT 1", (scope, kind, name))
        if last and last[0]["fingerprint"] == fp:
            self._write("UPDATE object_versions SET checked_at=? WHERE rowid=?", (now, last[0]["rowid"]))
            return False
        self._write("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (scope, kind, name, component, now, now, last[0]["checked_at"] if last else None, modified_at, fp,
                     json.dumps(content, sort_keys=True)))
        return True

    def versions(self, scope: str, kind: str, name: str, start: float, end: float) -> list[dict]:
        """Versions observed in [start, end], preceded by the last version observed before `start` (the baseline)."""
        before = self._read("SELECT * FROM object_versions WHERE scope=? AND kind=? AND name=? AND observed_at<? "
                            "ORDER BY observed_at DESC, rowid DESC LIMIT 1", (scope, kind, name, start))
        within = self._read("SELECT * FROM object_versions WHERE scope=? AND kind=? AND name=? AND observed_at BETWEEN ? AND ? "
                            "ORDER BY observed_at, rowid", (scope, kind, name, start, end))
        return [{**dict(r), "content": json.loads(r["content"])} for r in list(before) + list(within)]

    # ------------------------------------------------------------------ retention
    def prune(self, older_than: float) -> None:
        for sql in ("DELETE FROM log_lines WHERE COALESCE(t, observed_at)<?", "DELETE FROM log_gaps WHERE minute<?",
                    "DELETE FROM lifecycle WHERE COALESCE(t, observed_at)<?", "DELETE FROM events WHERE observed_at<?",
                    "DELETE FROM instances WHERE COALESCE(gone_at, last_seen)<?", "DELETE FROM sessions WHERE last_seen_at<?"):
            self._write(sql, (older_than,))
        # Keep the newest version of every object (it is the baseline for future diffs).
        self._write("DELETE FROM object_versions WHERE observed_at<? AND rowid NOT IN (SELECT MAX(rowid) FROM "
                    "object_versions GROUP BY scope, kind, name)", (older_than,))

    def stats(self) -> dict:
        out = {}
        for table in ("sessions", "instances", "lifecycle", "log_lines", "log_gaps", "events", "object_versions"):
            out[table] = self._read(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        return out
