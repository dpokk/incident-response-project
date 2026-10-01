"""Kubernetes evidence recorder (Iteration 4): keeps evidence that Kubernetes itself forgets.

Kubernetes keeps logs of only the current and the previous run of a container, loses everything about a pod
once it is deleted, expires events after about an hour, and keeps only the current value of a ConfigMap.
This recorder runs while the system runs (started by `watch`, or alone with `record`) and copies what it
observes into the provider-neutral HistoryStore:

  * pod lifecycle (watched): creation, readiness changes, every container termination with the run it ended,
    deletion
  * log lines of every run of every container (polled): each poll reads the lines a running container wrote
    since the last poll, keeping only lines from the current run's start time onwards; when a restart is
    seen, the ended run's logs are fetched as "previous" while Kubernetes still has them
  * events, configuration (ConfigMaps; Secrets only as fingerprints) and workload definitions (polled)

Known limit: lines a pod writes in its last poll interval (~5 s) before it is *deleted* (not restarted) are
lost, because Kubernetes discards a deleted pod's logs at once.

It is read-only towards the cluster. The KubernetesAdapter reads the store back to serve capabilities.
"""
import threading
import time

from ..history_store import HistoryStore, fingerprint

PROVIDER = "kubernetes"
MAX_LOG_LOOKBACK_S = 300     # never ask for more than this much log history in one read
BACKFILL_ATTEMPTS = 3        # polls on which an ended run's logs are fetched if they were still empty


class KubernetesRecorder:
    def __init__(self, kube, namespace: str, store: HistoryStore, clock=time.time, poll_s: float = 5.0,
                 retention_s: float = 24 * 3600, log=print):
        self.kube, self.ns, self.store, self.clock = kube, namespace, store, clock
        self.poll_s, self.retention_s, self.log = poll_s, retention_s, log
        self.pods: dict[str, dict] = {}
        self.workloads: list[dict] = []
        self.last_line: dict[tuple, float] = {}       # (pod, container, generation) -> newest stored line time
        self.backfills: list[tuple] = []              # ended runs whose logs still need fetching
        self.session: int | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_prune = 0.0
        self._last_poll_t: float | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self, threads: bool = True) -> None:
        self.session = self.store.open_session(PROVIDER, self.ns)
        self._prune()
        self.workloads = self.kube.workloads(self.ns)
        for p in self.kube.pods(self.ns):
            self.on_pod("ADDED", p, initial=True)
        self.poll_once()
        if threads:
            # One loop does everything. (A pod watch was tried first: with this environment's HTTP client the
            # watch delivered its events only when it closed, ~60 s late - too late to fetch an ended run's logs
            # before the pod was deleted. Comparing pod snapshots every poll is simpler and timely.)
            threading.Thread(target=self._poll_loop, daemon=True, name="recorder-poll").start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - keep recording through transient API errors
                self.log(f"recorder poll failed: {exc!r}")

    # ------------------------------------------------------------------ pods
    def component_of(self, pod: dict) -> str | None:
        for w in self.workloads:
            sel = w.get("selector") or {}
            if sel and all(pod["labels"].get(k) == v for k, v in sel.items()):
                return w["name"]
        return pod.get("app")

    def on_pod(self, etype: str, pod: dict, initial: bool = False) -> None:
        name, now, comp = pod["name"], self.clock(), self.component_of(pod)
        with self._lock:
            prev = self.pods.get(name)
            if etype == "DELETED":
                self.pods.pop(name, None)
            else:
                self.pods[name] = pod
        self.store.upsert_instance(self.ns, name, comp, "Pod", pod.get("created"))
        if etype == "DELETED":
            # Seen missing at this poll: it went away after the previous poll and no later than now.
            self.store.mark_gone(self.ns, name, now)
            self.store.add_lifecycle(self.ns, comp, name, None, "deleted", now, "observed",
                                     not_before=self._last_poll_t)
            return
        if prev is None and not initial:
            self.store.add_lifecycle(self.ns, comp, name, None, "created", pod.get("created") or now,
                                     "exact" if pod.get("created") else "observed")
        if prev is not None and pod["ready"] != prev["ready"]:
            self.store.add_lifecycle(self.ns, comp, name, None, "ready" if pod["ready"] else "not_ready",
                                     pod.get("ready_since") or now, "exact" if pod.get("ready_since") else "observed")
        prev_c = {c["name"]: c for c in (prev or {}).get("containers", [])}
        for c in pod["containers"]:
            pc = prev_c.get(c["name"])
            term = c.get("last_state") or {}
            restarted = pc is not None and c["restart_count"] > pc["restart_count"]
            # A termination still shown when recording starts, or a new one: either way it is evidence.
            if term.get("state") == "terminated" and (restarted or (prev is None and c["restart_count"] > 0)):
                gen = c["restart_count"] - 1
                self.store.add_lifecycle(
                    self.ns, comp, name, c["name"], "terminated", term.get("finished_at") or now,
                    "exact" if term.get("finished_at") else "observed", generation=gen, reason=term.get("reason"),
                    exit_code=term.get("exit_code"), started_at=term.get("started_at"), restart_count=c["restart_count"],
                    unrecorded_terminations=(c["restart_count"] - pc["restart_count"] - 1) if restarted else 0)
                with self._lock:
                    self.backfills.append((name, comp, c["name"], gen, 0))
            st, pst = c.get("state") or {}, (pc or {}).get("state") or {}
            if st.get("state") == "waiting" and st.get("reason") != pst.get("reason"):
                self.store.add_lifecycle(self.ns, comp, name, c["name"], "waiting", now, "observed",
                                         reason=st.get("reason"), message=(st.get("message") or "")[:300])

    def _current_generation(self, pod: str, container: str) -> int | None:
        with self._lock:
            p = self.pods.get(pod)
        c = next((c for c in (p or {}).get("containers", []) if c["name"] == container), None)
        return c["restart_count"] if c else None

    # ------------------------------------------------------------------ logs
    def backfill_previous(self, pod: str, comp: str | None, container: str, generation: int) -> int | None:
        """Fetch an ended run's logs while Kubernetes still has them. Returns None (give up) if the container has
        restarted again meanwhile: "previous" would then be a different run, and mislabelled evidence is worse
        than missing evidence. Returns 0 if Kubernetes had nothing yet (worth retrying)."""
        if self._current_generation(pod, container) != generation + 1:
            return None
        lines = self.kube.logs(self.ns, pod, container, previous=True)
        if self._current_generation(pod, container) != generation + 1:
            return None
        return self.store.add_log_lines(self.ns, comp, pod, container, generation, lines) if lines else 0

    def capture_current(self, pod: dict) -> int:
        """Store what each running container of `pod` wrote since the last capture. Only lines from the current
        run's start onwards are labelled with the current run; earlier ones belong to the previous run."""
        stored, comp = 0, self.component_of(pod)
        for c in pod["containers"]:
            st = c.get("state") or {}
            if st.get("state") != "running":
                continue
            key = (pod["name"], c["name"], c["restart_count"])
            started = st.get("started_at")
            since_t = self.last_line.get(key) or started or (self.clock() - MAX_LOG_LOOKBACK_S)
            since_s = min(MAX_LOG_LOOKBACK_S, max(1.0, self.clock() - since_t + 2))   # overlap; duplicates ignored
            lines = [(t, line) for t, line in self.kube.logs(self.ns, pod["name"], c["name"], since_s=since_s)
                     if t is None or started is None or t >= started - 1]
            if lines:
                stored += self.store.add_log_lines(self.ns, comp, pod["name"], c["name"], c["restart_count"], lines)
                self.last_line[key] = max((t for t, _ in lines if t), default=self.last_line.get(key))
        return stored

    # ------------------------------------------------------------------ the poll
    def poll_once(self) -> None:
        self.workloads = self.kube.workloads(self.ns)
        pods = self.kube.pods(self.ns)
        live = {p["name"] for p in pods}
        # Lifecycle from snapshots: on_pod compares each pod with the previous snapshot (restarts, readiness,
        # waiting); pods missing from this snapshot were deleted since the last poll.
        for p in pods:
            self.on_pod("MODIFIED" if p["name"] in self.pods else "ADDED", p)
        for name in [n for n in self.pods if n not in live]:
            self.on_pod("DELETED", self.pods[name])
        # Ended runs' logs right away, while Kubernetes still has them; an empty read is retried next polls.
        with self._lock:
            pending, self.backfills = self.backfills, []
        for pod, comp, container, gen, attempts in pending:
            if self.backfill_previous(pod, comp, container, gen) == 0 and attempts < BACKFILL_ATTEMPTS - 1:
                with self._lock:
                    self.backfills.append((pod, comp, container, gen, attempts + 1))
        for p in pods:
            self.capture_current(p)
        for e in self.kube.events(self.ns):
            comp = self._event_component(e["object_kind"], e["object_name"])
            self.store.upsert_event(self.ns, comp, e["object_kind"], e["object_name"], e["type"], e["reason"],
                                    e["message"], e["count"], e["first"], e["last"])
        for cm in self.kube.configmaps(self.ns):
            self.store.add_version(self.ns, "ConfigMap", cm["name"], None, cm["data"], modified_at=cm.get("last_modified"))
        for name in sorted({ef["secret"] for w in self.workloads for c in w["containers"] for ef in c["env_from"]
                            if "secret" in ef} | {e["from"]["secret"] for w in self.workloads for c in w["containers"]
                                                  for e in c["env"] if e["from"] and "secret" in e["from"]}):
            values = self.kube.secret_values(self.ns, name) or {}
            self.store.add_version(self.ns, "Secret", name, None, {k: fingerprint(v) for k, v in values.items()})
        for w in self.workloads:
            self.store.add_version(self.ns, w["kind"], w["name"], w["name"], _definition(w))
        self.last_line = {k: v for k, v in self.last_line.items() if k[0] in live}
        self._last_poll_t = self.clock()
        if self.session is not None:
            self.store.heartbeat(self.session)
        if self.clock() - self._last_prune > 3600:
            self._prune()

    def _event_component(self, kind: str, name: str) -> str | None:
        if kind in ("Deployment", "StatefulSet"):
            return name
        if kind == "Pod":
            with self._lock:
                pod = self.pods.get(name)
            return self.component_of(pod) if pod else self.store.component_of(self.ns, name)
        return next((w["name"] for w in self.workloads if name.startswith(w["name"] + "-")), None)

    def _prune(self) -> None:
        self._last_prune = self.clock()
        self.store.prune(self._last_prune - self.retention_s)


def _definition(w: dict) -> dict:
    """The parts of a workload definition whose change can explain an incident (no status fields)."""
    return {"replicas": w["replicas_desired"],
            "containers": [{"name": c["name"], "image": c["image"], "limits": c["limits"], "requests": c["requests"],
                            "env": [{"name": e["name"], "value": e["value"], "from": e["from"]} for e in c["env"]],
                            "env_from": c["env_from"]} for c in w["containers"]]}
