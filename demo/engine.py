"""The demo engine: connects the live cluster view, detection, the rule-based investigation, the Investigator Agent,
Slack, human approval, deterministic execution and verification - and publishes every real step to the page.

Lifecycle (one incident at a time):
    stopped → monitoring → detected → investigating → agent → awaiting_approval (in Slack)
            → approved (Execute pending in Slack) → remediating → resolved | not_resolved | rejected | failed
            → (reset) → monitoring

The engineer approves and executes in Slack (the existing Iteration 6/7 buttons); the page only mirrors it.

Every component used here already exists in the investigator (Iterations 2-7). This module only wires them together
and reports what they do. Nothing shown on the page is simulated.
"""
import subprocess
import threading
import time
import traceback
from pathlib import Path

from investigator import pipeline, slack
from investigator.context import IncidentContext
from investigator.detector import Detector
from investigator.evidence import EvidenceStore
from investigator.planner import plan_and_collect
from investigator.review import PARAMETER_SPECS, InvalidParameter, ReviewService

from . import slack_ai
from .agent.investigator import InvestigatorAgent
from .agent.llm import ChatModel
from .agent.tools import ToolBox
from .live import LogTailer, component_status, traffic

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = {
    "db-misconfig": {"title": "Wrong database host", "script": "scripts/inject/db-misconfig.ps1",
                     "what": "Changes backend's DATABASE_URL in its ConfigMap to a host that does not exist "
                             "(postgres-wrong) and rolls the backend out - like a bad configuration deploy."},
    "db-down": {"title": "Database down", "script": "scripts/inject/db-down.ps1",
                "what": "Scales the PostgreSQL deployment to 0 replicas. The backend's configuration stays correct."},
    "oom": {"title": "Memory exhaustion (traffic spike)", "script": "scripts/inject/oom.ps1",
            "what": "Sends a 90 s traffic spike (up to 800 req/s). The backend's request backlog grows past its 192Mi "
                    "memory limit and it is OOM-killed. The spike ends by itself after 90 s."},
}
TERMINAL = {"resolved", "not_resolved", "rejected", "failed"}


class TracingStore(EvidenceStore):
    """The investigation's own EvidenceStore, also reporting each planner decision and capability call live."""

    def __init__(self, emit, clock):
        super().__init__(clock=clock)
        self._emit = emit

    def step(self, name: str, detail: str = "", **info) -> None:
        super().step(name, detail, **info)
        if name == "decision":
            self._emit("rule_step", {"kind": "decision", "text": detail, "reason": info.get("reason", "")})
        elif name == "capability":
            self._emit("rule_step", {"kind": "call", "capability": detail, "args": info.get("args"),
                                     "status": info.get("status"), "ms": info.get("ms"),
                                     "summary": info.get("summary")})


class Engine:
    def __init__(self, settings, bus, log=print):
        self.s, self.bus, self.log = settings, bus, log
        self.phase = "stopped"
        self.providers = self.review = self.execution = None
        self.transport = slack.Transport(settings, log)
        self.registry = slack.ThreadRegistry(settings.slack_threads_path)
        self.tailer = LogTailer(bus, settings.kube_context)
        self.tailer.start()
        self.system: dict | None = None
        self.incident: dict | None = None
        self.ops: list[dict] = []
        self.busy: str | None = None                  # a running operation (start / trigger / reset)
        self.cooldown_until = 0.0
        self.lock = threading.RLock()
        self.model = ChatModel.from_env()
        self._monitor: threading.Thread | None = None

    # ----------------------------------------------------------------------------- page support
    def snapshot(self) -> dict:
        return {"phase": self.phase, "system": self.system, "incident": self.incident,
                "logs": list(self.tailer.recent)[-150:], "ops": self.ops[-40:], "busy": self.busy,
                "incidents": [{"id": k, **v} for k, v in INCIDENTS.items()],
                "config": {"model": self.model.name if self.model else None,
                           "slack": self.transport.configured, "slack_threaded": self.transport.threaded,
                           "approvers": sorted(self.s.slack_approvers), "namespace": self.s.namespace,
                           "entry": f"{self.s.entry_service}{self.s.entry_path.split('?')[0]}"}}

    def _emit(self, type_: str, data: dict, keep: bool = True) -> None:
        self.bus.publish(type_, data, keep)

    def _phase(self, phase: str, **extra) -> None:
        self.phase = phase
        if self.incident is not None:
            self.incident["phase"] = phase
        self._emit("phase", {"phase": phase, **extra})

    def _op(self, text: str, level: str = "info") -> None:
        entry = {"t": time.time(), "text": text, "level": level}
        self.ops.append(entry)
        self._emit("op", entry, keep=False)

    # ----------------------------------------------------------------------------- start
    def start(self) -> None:
        if self.busy or self.phase != "stopped":
            return
        self.busy = "start"
        self._emit("busy", {"busy": self.busy}, keep=False)
        threading.Thread(target=self._start, daemon=True, name="start").start()

    def _start(self) -> None:
        try:
            from investigator import providers as providers_mod
            self._op("Checking the Kubernetes cluster …")
            if not self._cluster_up():
                self._op(f"Cluster not reachable: starting minikube profile '{self.s.kube_context}' (this takes a "
                         f"minute or two) …")
                self._run(["minikube", "start", "-p", self.s.kube_context], "minikube")
            self._op("Connecting to Kubernetes and Prometheus …")
            self.providers = providers_mod.connect(self.s, log=lambda m: self._op(str(m).strip()))
            self.review = ReviewService(self.s.reviews_path, self.s.slack_approvers)
            self.execution = pipeline.execution_service(self.s, self.providers, self.review)
            self.providers.new_resources().start_background_recording()
            self._op("Evidence history recorder started")
            if self.s.slack_app_token:
                try:
                    from investigator import slack_app
                    slack_app.start(self.s, self.review, log=lambda m: self._op(str(m)), execution=self.execution)
                except Exception as exc:  # noqa: BLE001 - Slack buttons are optional; the console still works
                    self._op(f"Slack listener not started: {type(exc).__name__}", "warning")
            self.tailer.running.set()
            self._monitor = threading.Thread(target=self._monitor_loop, daemon=True, name="monitor")
            self._monitor.start()
            self._phase("monitoring")
            self._op("Monitoring started: detection, live status and logs are running")
        except Exception as exc:  # noqa: BLE001
            self._op(f"Start failed: {type(exc).__name__}: {exc}", "error")
            self._phase("stopped")
        finally:
            self.busy = None
            self._emit("busy", {"busy": None}, keep=False)

    def _cluster_up(self) -> bool:
        try:
            from investigator.kube import Kube
            Kube(self.s.kube_context).namespace(self.s.namespace)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _run(self, cmd: list[str], label: str, cwd: Path = ROOT) -> int:
        """Run a real command and stream its output to the page."""
        self._op(f"$ {' '.join(cmd)}")
        p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        for line in p.stdout:
            if line.strip():
                self._op(f"[{label}] {line.rstrip()[:240]}")
        rc = p.wait()
        self._op(f"[{label}] exited with code {rc}", "info" if rc == 0 else "error")
        return rc

    def _script(self, rel: str) -> int:
        return self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / rel)],
                         Path(rel).stem)

    # ----------------------------------------------------------------------------- monitoring + detection
    def _monitor_loop(self) -> None:
        det = Detector(self.s, self.providers.new_resources(), self.providers.metrics, clock=self.providers.clock)
        while True:
            try:
                sample = det.sample()
                self.system = {"t": sample["t"], "components": component_status(sample, self.s.error_log_threshold),
                               "probe": {"ok": sample["probe"]["ok"], "total": sample["probe"]["total"],
                                         "last_status": (sample["probe"]["last"] or [None])[0]},
                               "errors": sample["errors"], "unhealthy": sample["unhealthy"],
                               "signals": [x["text"] for x in sample["signals"]],
                               "traffic": traffic(self.providers.metrics, self.s.entry_app, sample["t"])}
                self._emit("system", self.system, keep=False)
                self._detect(sample)
                self._mirror_slack()
            except Exception as exc:  # noqa: BLE001 - keep monitoring through API hiccups
                self._op(f"status sample failed: {type(exc).__name__}: {exc}", "warning")
            time.sleep(self.s.poll_interval_s)

    def _detect(self, sample: dict) -> None:
        with self.lock:
            if self.phase == "detected" and self.incident:
                seen = {(x["kind"], x["subject"]) for x in self.incident["signals"]}
                new = [x for x in sample["signals"] if (x["kind"], x["subject"]) not in seen]
                if new:
                    self.incident["signals"] += new[:10 - len(self.incident["signals"])]
                    self._emit("signals", {"signals": [x["text"] for x in self.incident["signals"]]})
                return
            if self.phase != "monitoring" or self.busy == "reset" or time.time() < self.cooldown_until:
                return
            if not sample["signals"]:
                return
            inc = pipeline.new_incident(sample["signals"], sample["t"], self.s.namespace)
            self.incident = {**inc, "phase": "detected", "trigger": self._last_trigger}
            self.bus.clear_history(INCIDENT_EVENTS)
            self._phase("detected")
            self._emit("incident", {"id": inc["id"], "detected_at": inc["detected_at"],
                                    "signals": [x["text"] for x in inc["signals"]],
                                    "investigate_in_s": self.s.investigate_delay_s})
            threading.Thread(target=self._handle_incident, daemon=True, name="incident").start()

    _last_trigger: str | None = None

    # ----------------------------------------------------------------------------- the incident
    def _handle_incident(self) -> None:
        inc = self.incident
        try:
            self._slack_post_root(inc)
            time.sleep(self.s.investigate_delay_s)               # let symptoms accumulate, as watch does
            with self.lock:
                self._phase("investigating")
            self._slack_note(inc, slack_ai.started(inc, self.model.name if self.model else None))
            report, digest = self._rule_investigation(inc)
            ai = self._agent(inc, report)
            proposal = self._proposal(report, ai)
            inc.update({"report_id": report["id"], "digest": digest, "proposal": proposal})
            self._emit("proposal", proposal)
            self._slack_report(inc, report, digest, ai, proposal)
            self._phase("awaiting_approval" if proposal.get("executable") else "investigated")
        except Exception as exc:  # noqa: BLE001
            self._op(f"Investigation failed: {type(exc).__name__}: {exc}", "error")
            self.log(traceback.format_exc())
            self._phase("failed", error=f"{type(exc).__name__}: {exc}")

    def _rule_investigation(self, inc: dict) -> tuple[dict, str]:
        clock = self.providers.clock
        end = clock()
        start = inc["detected_at"] - self.s.lookback_s
        self._emit("rule_start", {"window": [start, end]})
        store = TracingStore(lambda t, d: self._emit(t, d), clock)
        caps = self.providers.capabilities(store)
        ctx = IncidentContext.from_incident(inc, start, end, entry=(self.s.entry_service, self.s.entry_port,
                                                                   self.s.entry_path),
                                            metrics_target=self.s.entry_app)
        plan_and_collect(caps, ctx, log=lambda m: None)          # decisions/calls reach the page via TracingStore
        res = pipeline.diagnose_and_report(self.s, inc, store, (start, end), post_to_slack=False, ongoing=True)
        r = res["report"]
        plan = r["remediation_plan"]
        self._emit("rule_result", {
            "category": r.get("failure_category"), "label": r.get("failure_category_label"),
            "affected": (r.get("affected_component") or {}).get("name"),
            "root_cause_component": (r.get("root_cause_component") or {}).get("name"),
            "root_cause": r.get("likely_root_cause"), "confidence": r.get("confidence_label"),
            "confidence_value": r.get("confidence"), "facts": r.get("facts_collected"),
            "evidence": [{"id": e["id"], "text": e["text"]} for e in r.get("evidence", [])[:8]],
            "plan_assessment": plan.get("assessment"), "digest": res["digest"],
            "impact": ((r.get("impact") or {}).get("users") or {}).get("statement")})
        return r, res["digest"]

    def _agent(self, inc: dict, report: dict) -> dict:
        self._phase("agent")
        if self.model is None:
            self._emit("agent_error", {"error": "no model configured (NVIDIA_API_KEY missing)"})
            return {"status": "unavailable", "error": "no model configured"}
        caps = self.providers.capabilities(EvidenceStore(clock=self.providers.clock))
        tb = ToolBox(caps, self.s, report, inc["detected_at"] - self.s.lookback_s, clock=self.providers.clock)
        out = InvestigatorAgent(self.model, tb, lambda t, d: self._emit(t, d)).run(inc)
        if out.get("status") == "ok":
            rep = out["report"]
            rule_rcc = (report.get("root_cause_component") or {}).get("name")
            rep["agreement"] = {"rule_category": report.get("failure_category"),
                                "rule_label": report.get("failure_category_label"),
                                "rule_component": rule_rcc,
                                "category": rep.get("category") == report.get("failure_category"),
                                "component": rep.get("root_cause_component") == rule_rcc}
            self._emit("agent_result", {**rep, "usage": out.get("usage"), "seconds": out.get("seconds"),
                                        "turns": out.get("turns"), "tool_calls": out.get("tool_calls"),
                                        "model": out.get("model")})
        return out

    def _proposal(self, report: dict, ai: dict) -> dict:
        """Map the agent's suggested fix to an executable action in the deterministic plan (or say why not)."""
        plan = report["remediation_plan"]
        actions = plan["actions"]
        fix = ((ai or {}).get("report") or {}).get("suggested_fix") or {}
        source = "agent" if fix else "rules"
        want = fix.get("action") if fix else None
        idx = None
        for i, a in enumerate(actions):
            if a["type"] == "investigate_further":
                continue
            if want is None or a["type"] == want:
                idx = i
                break
        base = {"source": source, "agent_action": want, "agent_description": fix.get("description"),
                "plan_assessment": plan.get("assessment"), "plan_reason": plan.get("assessment_reason")}
        if idx is None:
            why = ("the agent recommends investigating further" if want == "investigate_only" else
                   f"the rule engine's plan has no eligible '{want}' action for the current state" if want else
                   "the plan contains no executable action")
            return {**base, "executable": False, "reason": why,
                    "investigate": [s["statement"] for a in actions if a["type"] == "investigate_further"
                                    for s in a["rationale"]][:4]}
        a = actions[idx]
        spec = PARAMETER_SPECS.get(a["type"]) if not a["parameters_complete"] else None
        value, value_error = fix.get("parameter_value") if fix else None, None
        if spec and value:
            try:
                value = str(spec.parse(str(value), a))
            except InvalidParameter as exc:
                value_error, value = f"the agent's value '{fix.get('parameter_value')}' was rejected: {exc}", None
        return {**base, "executable": True, "plan_index": idx, "action_type": a["type"], "target": a["target"],
                "summary": a["summary"], "urgency": a["urgency"],
                "parameters": {k: v for k, v in a["parameters"].items() if v is not None and k != "questions"},
                "parameter": ({"name": spec.name, "description": spec.description, "value": value,
                               "source": "agent" if value else None, "error": value_error} if spec else None),
                "risks": [r["statement"] for r in a["risks"]][:4],
                "verification": [v["statement"] for v in a["verification"]],
                "rollback": (a.get("rollback") or {}).get("statement")}

    # ----------------------------------------------------------------------------- human decision (in Slack)
    def _mirror_slack(self) -> None:
        """Approval and execution happen in Slack (Iteration 6/7 buttons). The page mirrors the shared review and
        execution records - whoever clicked - so it always shows what really happened."""
        inc = self.incident
        if not inc or not inc.get("digest") or self.phase in ("resolved", "not_resolved", "rejected", "failed"):
            return
        idx = (inc.get("proposal") or {}).get("plan_index")
        if idx is None:
            return
        d = self.review.effective_decision(inc["id"], inc["digest"], idx)
        if d and inc.get("mirror_decision") != d["id"]:
            inc["mirror_decision"] = d["id"]
            self._emit("decision", {"decision": d["decision"], "by": d["reviewer"], "at": d["at"],
                                    "value": d.get("supplied_parameters") or {}})
            if d["decision"] == "approved":
                self._phase("approved")
            elif d["decision"] == "rejected":
                self._phase("rejected")
            else:
                self._phase("rejected", reason=f"sent back: {d['decision']}")
        attempts = [a for a in self.execution.store.attempts(inc["id"]) if a["plan_digest"] == inc["digest"]
                    and a["action_index"] == idx]
        if attempts and inc.get("mirror_attempt") != attempts[-1]["id"]:
            inc["mirror_attempt"] = attempts[-1]["id"]
            a = attempts[-1]
            if a["code"] != "claimed":
                self._emit("execute_refused", {"by": a["executor"], "code": a["code"], "message": a["message"],
                                               "at": a["at"]})
        rec = self.execution.store.for_action(inc["id"], inc["digest"], idx)
        if not rec:
            return
        key = (rec["status"], rec.get("outcome"), bool(rec.get("dry_run")), bool(rec.get("applied")),
               bool(rec.get("verification")))
        if inc.get("mirror_exec") == key:
            return
        inc["mirror_exec"] = key
        checks = ([{**c, "check": "policy:" + c["check"]} for c in (rec.get("policy") or {}).get("checks", [])]
                  + [{**c, "check": "recheck:" + c["check"]} for c in (rec.get("recheck") or {}).get("checks", [])])
        if self.phase in ("awaiting_approval", "approved"):
            self._phase("remediating")
        self._emit("remediation", {"stage": rec["status"], "checks": checks, "record": _public(rec),
                                   "requested_at": rec.get("requested_at"),
                                   "window": {"settle_max_s": self.execution.policy.verification.settle_max_s,
                                              "window_s": self.execution.policy.verification.bounded()}})
        if rec["status"] == "completed":
            self._emit("outcome", {"code": rec["outcome"], "message": rec.get("message"), "record": _public(rec)})
            if rec["outcome"] == "RESOLVED":
                self._phase("resolved")
                self._slack_note(inc, slack_ai.resolved(inc, rec))
            else:
                self._phase("not_resolved", outcome=rec["outcome"])
        elif rec["status"] in ("refused", "apply_failed", "uncertain"):
            self._phase("failed", error=rec.get("message"))

    # ----------------------------------------------------------------------------- operator actions
    def trigger(self, key: str) -> dict:
        if key not in INCIDENTS:
            return {"ok": False, "error": "unknown incident"}
        if self.phase != "monitoring" or self.busy:
            return {"ok": False, "error": f"cannot trigger now (phase: {self.phase}, busy: {self.busy})"}
        self.busy = f"trigger:{key}"
        self._last_trigger = key
        self._emit("busy", {"busy": self.busy}, keep=False)
        self._op(f"Triggering incident: {INCIDENTS[key]['title']} ({INCIDENTS[key]['script']})")

        def run():
            try:
                self._script(INCIDENTS[key]["script"])
            finally:
                self.busy = None
                self._emit("busy", {"busy": None}, keep=False)
        threading.Thread(target=run, daemon=True, name="trigger").start()
        return {"ok": True}

    def reset(self) -> dict:
        if self.phase == "stopped" or self.busy:
            return {"ok": False, "error": f"cannot reset now (phase: {self.phase}, busy: {self.busy})"}
        if self.phase in ("detected", "investigating", "agent", "remediating"):
            return {"ok": False, "error": f"wait until the current step finishes (phase: {self.phase})"}
        self.busy = "reset"
        self._emit("busy", {"busy": self.busy}, keep=False)

        def run():
            try:
                self._op("Restoring the healthy baseline (scripts/restore.ps1) …")
                self._script("scripts/restore.ps1")
                self._run(["kubectl", "--context", self.s.kube_context, "-n", self.s.namespace, "patch", "deploy/backend", "--type", "strategic", "-p",
                           '{"spec":{"template":{"spec":{"containers":[{"name":"backend","resources":{"limits":'
                           '{"memory":"192Mi"}}}]}}}}'], "baseline")
                with self.lock:
                    self.incident = None
                    self.bus.clear_history(INCIDENT_EVENTS)
                    self.cooldown_until = time.time() + 60
                    self._phase("monitoring")
                self._emit("incident_cleared", {}, keep=False)
                self._op("Baseline restored. Detection resumes in 60 s, once the restart effects have settled.")
            finally:
                self.busy = None
                self._emit("busy", {"busy": None}, keep=False)
        threading.Thread(target=run, daemon=True, name="reset").start()
        return {"ok": True}

    # ----------------------------------------------------------------------------- Slack
    def _slack_post_root(self, inc: dict) -> None:
        if not self.transport.configured:
            return
        slack.publish_detection(self.transport, self.registry, inc, self.s.namespace)
        self._emit("slack", {"text": f"Incident {inc['id']} posted to Slack (thread started)"})

    def _slack_note(self, inc: dict, payload: dict) -> None:
        if not self.transport.configured:
            return
        root = self.registry.get(inc["id"]).get("root_ts")
        ok = self.transport.post(payload, thread_ts=root) is not None
        self._emit("slack", {"text": payload.get("text", "")[:160], "ok": ok})

    def _slack_report(self, inc, report, digest, ai, proposal) -> None:
        if not self.transport.configured:
            return
        self._slack_note(inc, slack_ai.report(inc, ai, proposal, report))
        ok = slack.publish_investigation(self.transport, self.registry, report, self.review, digest,
                                         self.s.namespace, time.time(), execution=self.execution)
        self._emit("slack", {"text": "Rule-based investigation and the remediation plan (with Approve / Execute "
                                     "buttons) posted to the thread", "ok": ok})
        entry = self.registry.get(inc["id"])
        if entry.get("channel") and entry.get("root_ts"):
            link = self.transport._api("chat.getPermalink", {"channel": entry["channel"],
                                                              "message_ts": entry["root_ts"]})
            if link.get("ok"):
                inc["slack_url"] = link["permalink"]
                self._emit("slack_link", {"url": link["permalink"]})


INCIDENT_EVENTS = {"phase", "incident", "signals", "rule_start", "rule_step", "rule_result", "agent_start",
                   "agent_thinking", "agent_note", "agent_tool_call", "agent_tool_result", "agent_report",
                   "agent_result", "agent_error", "proposal", "decision", "remediation", "outcome", "slack",
                   "slack_link", "execute_refused"}


def _public(rec: dict | None) -> dict | None:
    """An execution record for the page (no internal-only fields)."""
    if not rec:
        return None
    keep = ("execution_id", "status", "outcome", "message", "approver", "executor", "change", "policy", "recheck",
            "dry_run", "applied", "verification", "refusal")
    return {k: rec.get(k) for k in keep}
