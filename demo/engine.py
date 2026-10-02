"""The demo engine: connects the live cluster view, detection, the rule-based investigation, the Investigator Agent,
Slack, human approval, deterministic execution and verification - and publishes every real step to the page.

Lifecycle (one incident at a time):
    stopped → monitoring → detected → investigating → agent →
        policy gate passed:  auto_remediating (the Remediation Agent) → resolved | handed_over → acknowledged
        otherwise:           awaiting_approval (in Slack) → approved (Execute pending in Slack) → remediating
                             → resolved | not_resolved | rejected | failed
    → (reset) → monitoring

The engineer approves and executes in Slack (the existing Iteration 6/7 buttons); the page only mirrors it. For a
known, high-confidence incident whose fix the policy allows to automate, the deterministic Remediation Agent
(remediation_agent.py) takes the two clicks' place and drives the same executor; an engineer can Stop it, and must
acknowledge when it hands over.

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

from . import agent_plan, slack_ai, slack_listener
from .remediation_agent import AGENT_ID, AutoPolicy, AutoStore, RemediationAgent, gate, ladder
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
    "db-credentials": {"title": "Stale database credentials", "script": "scripts/inject/db-credentials.ps1",
                       "what": "A credential rotation gone wrong: the backend is switched to read its database password "
                               "from a new Secret holding a stale value. PostgreSQL stays healthy; every query is rejected. "
                               "The rule engine has no specific pattern for this one."},
    "backend-down": {"title": "Backend scaled to zero", "script": "scripts/inject/backend-down.ps1",
                     "what": "A mistaken scale-down: the backend deployment goes to 0 replicas, so the frontend has "
                             "nothing to call. The rule engine has no pattern for a missing intermediate service, so the "
                             "Investigator Agent leads and an engineer gets its manual steps."},
    "memory-limit-low": {"title": "Memory limit set too low (bad deploy)", "script": "scripts/inject/memory-limit-low.ps1",
                         "what": "A configuration deploy lowers the backend's memory limit to 32Mi on all pods at once; normal traffic needs "
                                 "about 37 MB, so the new pods are OOM-killed and crash-loop. A higher limit is the real "
                                 "fix: the Remediation Agent's memory ladder can show more than one attempt."},
    "app-crash": {"title": "Application crash (bad data)", "script": "scripts/inject/app-crash.ps1",
                  "what": "One order with quantity 0. The backend's invoice job divides by zero, the process dies, and "
                          "because the order stays pending the pods crash-loop. No typed action fixes bad data."},
    "bad-image": {"title": "Broken release (missing image)", "script": "scripts/inject/bad-image.ps1",
                  "what": "A release points the backend at an image tag that does not exist. The new pod cannot pull "
                          "it and the rollout stalls while the old pods keep serving. An engineer must roll back."},
    "overload": {"title": "Sustained overload (automation refuses)", "script": "scripts/inject/overload.ps1",
                 "what": "800 req/s for 15 minutes: the backend is OOM-killed again and again, and a higher memory limit "
                         "would only delay it. In our runs the rule engine is not confident (under 50%) and its plan "
                         "offers no memory change, so the Remediation Agent's gate refuses and an engineer decides."},
}
KNOWN_CONFIDENCE = 0.75          # rules at or above this confidence lead; below it, the agent leads
BUDGETS = {"verify": (5, 150), "lead": (14, 300)}   # (tool calls, seconds)


def route(report: dict) -> dict:
    """Deterministic hybrid routing. Known pattern (a rule category at high confidence): the rules lead and the agent
    verifies with a small budget. Otherwise (undetermined, or low/medium confidence): the agent leads."""
    cat, conf = report.get("failure_category"), float(report.get("confidence") or 0)
    label = report.get("failure_category_label") or "undetermined"
    if not cat or cat == "undetermined":
        mode, reason = "lead", "the rule engine found no established cause"
    elif conf < KNOWN_CONFIDENCE:
        mode, reason = "lead", f"the rule engine is only {conf:.0%} confident ({label}): not a known pattern"
    else:
        mode, reason = "verify", f"known pattern: {label} at {conf:.0%} confidence"
    calls, secs = BUDGETS[mode]
    return {"mode": mode, "reason": reason, "rule_label": label, "rule_confidence": conf,
            "budget": {"tool_calls": calls, "seconds": secs}}
TERMINAL = {"resolved", "not_resolved", "rejected", "failed", "handed_over", "acknowledged"}
AUTO_POLICY_PATH = ROOT / "demo" / "remediation_agent_policy.json"


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
        self.auto_policy = AutoPolicy.load(AUTO_POLICY_PATH)
        self.auto_enabled = self.auto_policy.enabled
        self.auto_store: AutoStore | None = None
        self.auto: RemediationAgent | None = None
        self._auto_msgs: dict = {}                    # Slack (channel, ts) of the agent's evolving messages
        self.baseline_at = 0.0                        # when "Reset to healthy" last restored the baseline

    # ----------------------------------------------------------------------------- page support
    def snapshot(self) -> dict:
        return {"phase": self.phase, "system": self.system, "incident": self.incident,
                "logs": list(self.tailer.recent)[-150:], "ops": self.ops[-40:], "busy": self.busy,
                "incidents": [{"id": k, **v} for k, v in INCIDENTS.items()],
                "config": {"model": self.model.name if self.model else None,
                           "slack": self.transport.configured, "slack_threaded": self.transport.threaded,
                           "approvers": sorted(self.s.slack_approvers), "namespace": self.s.namespace,
                           "entry": f"{self.s.entry_service}{self.s.entry_path.split('?')[0]}",
                           "auto": self.auto_config()}}

    def auto_config(self) -> dict:
        return {"enabled": self.auto_enabled, "min_rule_confidence": self.auto_policy.min_rule_confidence,
                "actions": {k: v.get("max_attempts", 1) for k, v in self.auto_policy.actions.items()},
                "locked": sorted((self.auto_store.locked() if self.auto_store else {}).keys())}

    def set_auto(self, enabled: bool) -> dict:
        self.auto_enabled = bool(enabled)
        self._op("Automatic remediation " + ("ON: the Remediation Agent may act on known, high-confidence incidents"
                                             if enabled else "OFF: every remediation needs an engineer in Slack"))
        self._emit("auto_config", self.auto_config(), keep=False)
        return {"ok": True, **self.auto_config()}

    def _actor(self, name: str, state: str, text: str) -> None:
        """Who is working on the incident right now (the hand-off strip on the page)."""
        self._emit("actor", {"name": name, "state": state, "text": text, "t": time.time()})

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
            # The Remediation Agent is a configured approver/executor identity (never a Slack user), so its
            # automatic decisions pass exactly the same checks as a person's and are audited the same way.
            self.review = ReviewService(self.s.reviews_path, set(self.s.slack_approvers) | {AGENT_ID})
            self.execution = pipeline.execution_service(self.s, self.providers, self.review)
            self.auto_store = AutoStore(self.s.state_dir / "remediation_agent.db")
            self.auto = RemediationAgent(self.review, self.execution, self.auto_store, self._emit, self._auto_say,
                                         log=self.log)
            self.providers.new_resources().start_background_recording()
            self._op("Evidence history recorder started")
            if self.s.slack_app_token:
                try:
                    slack_listener.start(self.s, self.review, self.execution, self._on_auto_click,
                                         log=lambda m: self._op(str(m)))
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
            self._actor("detector", "done", f"opened {inc['id']} from {len(inc['signals'])} signal(s); collecting "
                                            f"symptoms for {self.s.investigate_delay_s:.0f} s")
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
            self._actor("rules", "working", "collecting evidence through read-only capabilities")
            report, digest = self._rule_investigation(inc)
            r = route(report)
            inc["route"] = r
            self._actor("rules", "done", f"{report.get('failure_category_label')} at "
                                         f"{float(report.get('confidence') or 0):.0%} confidence")
            self._emit("route", r)
            ai = self._agent(inc, report, r)
            digest = self._agent_plan(inc, report, ai, r) or digest
            proposal = self._proposal(report, ai, r)
            inc.update({"report_id": report["id"], "digest": digest, "proposal": proposal})
            self._emit("proposal", proposal)
            g = gate(self.auto_policy, self.auto_enabled, r, ai, proposal, self.auto_store.locked())
            values = ladder(self.auto_policy, proposal, self.execution.policy.max_memory_bytes) if g["eligible"] else []
            if g["eligible"] and not values:
                g["checks"].append({"check": "ladder", "ok": False, "detail": "no value can be tried within policy"})
                g["eligible"] = False
            inc["gate"] = g
            self._emit("auto_gate", {**g, "ladder": [str(v) for v in values]})
            self._slack_report(inc, report, digest, ai, proposal, g)
            if g["eligible"]:
                self._auto_remediate(inc, proposal, values, g)
                return
            failed = next((c["detail"] for c in g["checks"] if not c["ok"]), "")
            if proposal.get("executable"):
                self._actor("remediation", "standby", f"not automated: {failed}")
                self._actor("engineer", "waiting", "Approve, then Execute, in Slack")
                self._phase("awaiting_approval")
            else:
                self._actor("remediation", "standby", "no typed action fits: nothing to execute")
                self._actor("engineer", "waiting", "manual fix (the agent's steps are in Slack)")
                self._phase("investigated")
        except Exception as exc:  # noqa: BLE001
            self._op(f"Investigation failed: {type(exc).__name__}: {exc}", "error")
            self.log(traceback.format_exc())
            self._phase("failed", error=f"{type(exc).__name__}: {exc}")

    def _rule_investigation(self, inc: dict) -> tuple[dict, str]:
        clock = self.providers.clock
        end = clock()
        start = self._window_start(inc)
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

    def _window_start(self, inc: dict) -> float:
        """Evidence window start: the usual lookback, but never before the last reset - otherwise the previous
        (already restored) incident's kills and errors would be read as evidence for this one."""
        return max(inc["detected_at"] - self.s.lookback_s, self.baseline_at)

    def _agent(self, inc: dict, report: dict, r: dict) -> dict:
        self._phase("agent")
        if self.model is None:
            self._emit("agent_error", {"error": "no model configured (NVIDIA_API_KEY missing)"})
            self._actor("investigator", "offline", "no model configured: the rule engine's findings stand")
            return {"status": "unavailable", "error": "no model configured"}
        self._actor("investigator", "working", ("verifying the rule engine" if r["mode"] == "verify" else
                                                "leading the investigation") + f" (≤ {r['budget']['tool_calls']} "
                                               f"read-only tool calls, {r['budget']['seconds']} s)")
        caps = self.providers.capabilities(EvidenceStore(clock=self.providers.clock))
        tb = ToolBox(caps, self.s, report, self._window_start(inc), clock=self.providers.clock)
        inc["components"] = list(tb.components)
        rcc = (report.get("root_cause_component") or {}).get("name")
        summary = (f"{report.get('failure_category_label')} (root-cause component {rcc}; confidence "
                   f"{float(report.get('confidence') or 0):.0%}): {report.get('likely_root_cause')}")
        out = InvestigatorAgent(self.model, tb, lambda t, d: self._emit(t, d), mode=r["mode"], rule_summary=summary,
                                max_tool_calls=r["budget"]["tool_calls"],
                                max_seconds=r["budget"]["seconds"]).run(inc)
        if out.get("status") == "ok":
            rep = out["report"]
            rule_rcc = (report.get("root_cause_component") or {}).get("name")
            rep["led_by"] = "agent" if r["mode"] == "lead" else "rules"
            rep["agreement"] = {"rule_category": report.get("failure_category"),
                                "rule_label": report.get("failure_category_label"),
                                "rule_component": rule_rcc,
                                "category": rep.get("category") == report.get("failure_category"),
                                "component": rep.get("root_cause_component") == rule_rcc}
            self._emit("agent_result", {**rep, "usage": out.get("usage"), "seconds": out.get("seconds"),
                                        "turns": out.get("turns"), "tool_calls": out.get("tool_calls"),
                                        "model": out.get("model")})
            self._actor("investigator", "offline", f"report submitted after {out.get('tool_calls')} tool calls, "
                                                   f"{out.get('seconds')} s; it has no further role")
        else:
            self._actor("investigator", "offline", f"no report ({out.get('error') or out.get('status')}): the rule "
                                                   f"engine's findings stand")
        return out

    def _agent_plan(self, inc: dict, report: dict, ai: dict, r: dict) -> str | None:
        """When the agent led and proposed a typed scale/memory fix the rule engine's plan lacks, make it a reviewable
        plan (agent_plan.py) that supersedes the rules' plan. Returns the new digest, or None."""
        if r["mode"] != "lead" or (ai or {}).get("status") != "ok":
            return None
        caps = self.providers.capabilities(EvidenceStore(clock=self.providers.clock))
        pol = self.execution.policy
        plan, why = agent_plan.build(report["remediation_plan"], ai["report"], caps, inc.get("components") or [],
                                     time.time(), self.s.entry_app, pol.max_replicas, pol.max_memory_bytes)
        if plan is None:
            self._emit("agent_plan", {"created": False, "reason": why})
            return None
        digest = self.review.register_plan(inc["id"], plan, time.time())
        report["remediation_plan"] = plan
        self._emit("agent_plan", {"created": True, "summary": plan["actions"][0]["summary"], "digest": digest})
        self._op(f"The Investigator Agent's fix became a reviewable plan: {plan['actions'][0]['summary']} "
                 f"(needs Approve + Execute in Slack)")
        return digest

    def _proposal(self, report: dict, ai: dict, r: dict) -> dict:
        """The executable action always comes from the deterministic plan (its eligibility rules). The agent's
        suggestion selects among eligible actions and proposes the value. When the rules lead, an agent that
        declines to fix (or suggests an ineligible action) does not remove the rules' action; when the agent leads,
        its 'investigate only' stands and its manual steps are shown instead."""
        plan = report["remediation_plan"]
        actions = plan["actions"]
        rep = (ai or {}).get("report") or {}
        fix = rep.get("suggested_fix") or {}
        want = fix.get("action") if fix else None
        eligible = [i for i, a in enumerate(actions) if a["type"] != "investigate_further"]
        idx = next((i for i in eligible if actions[i]["type"] == want), None)
        source = "agent" if idx is not None else "rules"
        if idx is None and eligible and (r["mode"] == "verify" or not fix):
            idx = eligible[0]
        base = {"source": source, "agent_action": want, "agent_description": fix.get("description"),
                "manual_steps": rep.get("manual_steps") or [], "led_by": "agent" if r["mode"] == "lead" else "rules",
                "plan_assessment": plan.get("assessment"), "plan_reason": plan.get("assessment_reason")}
        if idx is None:
            why = ("the Investigator Agent found no safe typed action for this cause; manual steps below"
                   if want == "investigate_only" else
                   f"the rule engine's plan has no eligible '{want}' action for the current state" if want else
                   "the plan contains no executable action")
            return {**base, "executable": False, "reason": why,
                    "investigate": [s["statement"] for a in actions if a["type"] == "investigate_further"
                                    for s in a["rationale"]][:4]}
        a = actions[idx]
        spec = PARAMETER_SPECS.get(a["type"]) if not a["parameters_complete"] else None
        value, value_error = (fix.get("parameter_value") if fix and source == "agent" else None), None
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
        if not inc or not inc.get("digest") or inc.get("auto") or self.phase in TERMINAL:
            return
        idx = (inc.get("proposal") or {}).get("plan_index")
        if idx is None:                   # nothing executable: the engineer acknowledges and fixes it manually
            for d in self.review.decisions(inc["id"], effective_only=True):
                if d["plan_digest"] == inc["digest"] and d["decision"] == "acknowledged" \
                        and inc.get("mirror_decision") != d["id"]:
                    inc["mirror_decision"] = d["id"]
                    self._emit("decision", {"decision": "acknowledged", "by": d["reviewer"], "at": d["at"],
                                            "value": {}})
                    self._actor("engineer", "working", f"{d['reviewer']} acknowledged in Slack and owns the manual fix")
                    self._phase("acknowledged")
            return
        d = self.review.effective_decision(inc["id"], inc["digest"], idx)
        if d and inc.get("mirror_decision") != d["id"]:
            inc["mirror_decision"] = d["id"]
            self._emit("decision", {"decision": d["decision"], "by": d["reviewer"], "at": d["at"],
                                    "value": d.get("supplied_parameters") or {}})
            self._actor("engineer", "done" if d["decision"] != "approved" else "waiting",
                        f"{d['decision']} in Slack" + (": Execute next" if d["decision"] == "approved" else ""))
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
            self._actor("engineer", "done", "approved and executed in Slack")
            self._actor("remediation", "working", "executing the engineer's approved change (deterministic executor)")
        self._emit("remediation", {"stage": rec["status"], "checks": checks, "record": _public(rec),
                                   "requested_at": rec.get("requested_at"),
                                   "window": {"settle_max_s": self.execution.policy.verification.settle_max_s,
                                              "window_s": self.execution.policy.verification.bounded()}})
        if rec["status"] == "completed":
            self._emit("outcome", {"code": rec["outcome"], "message": rec.get("message"), "record": _public(rec)})
            self._actor("remediation", "offline", f"verification: {rec['outcome']}")
            if rec["outcome"] == "RESOLVED":
                self._phase("resolved")
                self._slack_note(inc, slack_ai.resolved(inc, rec))
            else:
                self._phase("not_resolved", outcome=rec["outcome"])
        elif rec["status"] in ("refused", "apply_failed", "uncertain"):
            self._actor("remediation", "offline", f"stopped: {rec['status']}")
            self._phase("failed", error=rec.get("message"))

    # ----------------------------------------------------------------------------- the Remediation Agent
    def _auto_remediate(self, inc: dict, proposal: dict, values: list, g: dict) -> None:
        inc["auto"] = True
        self._phase("auto_remediating")
        self._actor("remediation", "working", "policy gate passed: auto-approved; value ladder "
                    + " → ".join(map(str, values)))
        self._actor("engineer", "standby", "informed in Slack; can press Stop")
        out = self.auto.run(inc, proposal, values, g)
        inc["auto_result"] = out
        if out["result"] == "resolved":
            a = out["resolved_by"]
            self._actor("remediation", "offline", f"resolved on attempt {a['n']} of {a['of']} ({a.get('before')} → "
                                                  f"{a.get('after')}); nothing further to do")
            self._actor("engineer", "done", "no action needed")
            self._phase("resolved")
        elif out.get("stopped_by"):
            self._actor("remediation", "offline", "stopped by an engineer: no further attempt, no revert")
            self._actor("engineer", "working", f"{out['stopped_by']} owns the incident")
            self._phase("acknowledged")
        else:
            rv = out.get("revert") or {}
            self._actor("remediation", "offline", "handed over: " + (out.get("reason") or "")
                        + (f"; reverted to {rv.get('after')}" if rv.get("ok") else ""))
            self._actor("engineer", "waiting", f"acknowledge in Slack and fix manually ({out.get('component')} locked)")
            self._phase("handed_over")

    def _auto_say(self, kind: str, data: dict) -> None:
        """Render the Remediation Agent's messages into the incident's Slack thread (attempts update in place)."""
        inc = self.incident
        if not inc or not self.transport.configured:
            return
        try:
            if kind == "attempt":
                payload = slack_ai.auto_attempt(inc, data)
                if data["n"] == 1 and data.get("stage") in ("approved", "done"):   # the plan message shows it too
                    slack.refresh_plan(self.transport, self.registry, self.review, inc["id"], inc["digest"],
                                       time.time(), execution=self.execution)
                    slack.refresh_root(self.transport, self.registry, self.review, inc["id"], self.execution)
                slot = (inc["id"], "attempt", data["n"])
                if slot in self._auto_msgs and self.transport.update(*self._auto_msgs[slot], payload):
                    return
                res = self.transport.post(payload, thread_ts=self.registry.get(inc["id"]).get("root_ts"))
                if res is not None:
                    self._auto_msgs[slot] = res
                self._emit("slack", {"text": payload["text"][:160], "ok": res is not None})
                return
            payload = {"start": lambda: slack_ai.auto_start(inc, data["ladder"], data["proposal"], data["gate"]),
                       "revert": lambda: slack_ai.auto_revert(inc, data),
                       "handover": lambda: slack_ai.auto_handover(inc, data),
                       "resolved": lambda: slack_ai.auto_resolved(inc, data),
                       "stopped": lambda: slack_ai.auto_stopped(inc, data["by"])}.get(kind)
            if payload is not None:
                self._slack_note(inc, payload())
        except Exception as exc:  # noqa: BLE001 - Slack rendering never affects the remediation
            self._op(f"Slack message ({kind}) failed: {type(exc).__name__}: {exc}", "warning")

    def _on_auto_click(self, kind: str, incident_id: str, user: str) -> str | None:
        """Stop / Acknowledge from Slack. Only the configured (human) approvers may use them."""
        if user not in self.s.slack_approvers:
            return "Only a configured approver can do this; nothing was changed."
        inc = self.incident if self.incident and self.incident.get("id") == incident_id else None
        if kind == "stop":
            if self.auto and self.auto.stop(incident_id, user):
                self._op(f"{user} pressed Stop: the Remediation Agent makes no further change on {incident_id}")
                self._actor("engineer", "working", f"{user} pressed Stop and owns the incident")
                return None
            return "Automatic remediation is not running for this incident."
        if kind == "ack":
            comps = self.auto_store.acknowledge(incident_id, user) if self.auto_store else []
            if not comps:
                return "Nothing to acknowledge (already acknowledged)."
            self.auto_store.event(incident_id, "acknowledged", user, {"components": comps})
            self._emit("auto_ack", {"by": user, "components": comps, "at": time.time()})
            self._emit("auto_config", self.auto_config(), keep=False)
            note = slack_ai.auto_acknowledged(inc or {"id": incident_id}, user, comps)
            if inc:
                self._slack_note(inc, note)
                self._actor("engineer", "working", f"{user} acknowledged and is fixing it manually")
                if self.phase == "handed_over":
                    self._phase("acknowledged")
            elif self.transport.configured:
                self.transport.post(note, thread_ts=self.registry.get(incident_id).get("root_ts"))
            self._op(f"{user} acknowledged {incident_id}; automatic remediation re-enabled for {', '.join(comps)}")
            return None
        return None

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
        if self.phase in ("detected", "investigating", "agent", "remediating", "auto_remediating"):
            return {"ok": False, "error": f"wait until the current step finishes (phase: {self.phase})"}
        self.busy = "reset"
        self._emit("busy", {"busy": self.busy}, keep=False)

        def run():
            try:
                self._op("Restoring the healthy baseline (scripts/restore-extras.ps1, restore.ps1, "
                         "restore-credentials.ps1) …")
                self._script("scripts/restore-extras.ps1")       # first: restore.ps1 waits on the backend rollout
                self._script("scripts/restore.ps1")
                self._script("scripts/restore-credentials.ps1")
                with self.lock:
                    self.incident = None
                    self.bus.clear_history(INCIDENT_EVENTS)
                    self.cooldown_until = time.time() + 60
                    self.baseline_at = time.time()
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

    def _slack_report(self, inc, report, digest, ai, proposal, gate=None) -> None:
        if not self.transport.configured:
            return
        self._slack_note(inc, slack_ai.report(inc, ai, proposal, report, gate))
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
                   "slack_link", "execute_refused", "route", "actor", "auto_gate", "auto_start", "auto_attempt",
                   "auto_progress", "auto_revert", "auto_done", "auto_stop", "auto_ack", "agent_plan"}


def _public(rec: dict | None) -> dict | None:
    """An execution record for the page (no internal-only fields)."""
    if not rec:
        return None
    keep = ("execution_id", "status", "outcome", "message", "approver", "executor", "change", "policy", "recheck",
            "dry_run", "applied", "verification", "refusal")
    return {k: rec.get(k) for k in keep}
