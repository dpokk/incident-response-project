"""LLM reasoning layer (provider: NVIDIA-hosted OpenAI-compatible models, or Anthropic Claude).

The model receives the *structured* output of the deterministic analysis (timeline candidates,
scored hypotheses with their checks, impact numbers, grouped events/logs and an evidence
catalog) and produces the narrative conclusions as schema-constrained JSON. Evidence IDs it
cites are validated against the catalog. If no API key is configured or the call fails, a
template-based narrative is produced from the same analysis so the pipeline still completes.
"""
import json
import re
import time
from datetime import datetime

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "symptom": {"type": "string"},
        "root_cause": {"type": "string"},
        "causal_chain": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"step": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                "required": ["step", "evidence"],
                "additionalProperties": False,
            },
        },
        "contributing_factors": {"type": "array", "items": {"type": "string"}},
        "impact_summary": {"type": "string"},
        "ruled_out": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"hypothesis": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["hypothesis", "reason"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
        "confidence_rationale": {"type": "string"},
        "recommended_next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "symptom", "root_cause", "causal_chain", "contributing_factors",
                 "impact_summary", "ruled_out", "confidence", "confidence_rationale", "recommended_next_steps"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the reasoning component of an automated Kubernetes incident investigation system.

You receive structured evidence that has already been collected and pre-analysed: a reconstructed timeline, \
scored candidate hypotheses (each with the checks that passed or failed), impact figures, grouped Kubernetes \
events and application logs, and an evidence catalog where every item has an ID (E1, E2, ...).

Your job is to write the investigation conclusions for on-call engineers:
- Distinguish the observed symptom (what users/monitors saw) from the root cause (why it happened).
- Explain the causal chain step by step, in chronological order, and cite evidence IDs for every step. \
Only cite IDs that exist in the evidence catalog.
- Base every claim on the supplied evidence. If something is not supported by the evidence, do not state it \
as fact; call out gaps instead. Use the exact numbers and times given; do not invent values. For peak \
values (error rate, latency, memory) use the peak figures in "impact" and "signals", not the first threshold \
crossing reported in the timeline.
- A change (rollout/ConfigMap edit) close to the incident is a candidate cause: weigh it using the hypothesis \
checks (e.g. whether the service was healthy between the change and the failures) rather than dismissing or \
blaming it outright.
- Say which alternative hypotheses were ruled out and why, using the failed/passed checks.
- confidence is a number between 0 and 1. Start from the rule-engine confidence and adjust it only with a \
stated reason (e.g. missing evidence sources, contradictory signals).
- Recommended next steps are suggestions for engineers to consider (investigation follow-ups or preventive \
changes). Do not claim any remediation has been performed.
- Be concise and specific: this goes into a Slack incident report. Refer to times as HH:MM:SS."""


def _fmt(t):
    return datetime.fromtimestamp(t).astimezone().strftime("%H:%M:%S") if t else None


def build_llm_input(bundle: dict, a: dict) -> dict:
    inc = bundle["incident"]
    res_summary = {
        k: {kk: (_fmt(vv) if kk.endswith("_t") else (round(vv, 3) if isinstance(vv, float) else vv))
            for kk, vv in r.items()}
        for k, r in a["resources"].items()
    }
    return {
        "incident": {"id": inc["id"], "detected_at": _fmt(inc.get("detected_at")), "triggers": inc.get("triggers", []),
                     "namespace": a["target"]["namespace"], "timezone": datetime.now().astimezone().tzname()},
        "investigation_window": {"start": _fmt(bundle["window"]["start"]), "end": _fmt(bundle["window"]["end"])},
        "user_facing_service": a["entry_app"],
        "suspected_workload": {
            "deployment": a["target"]["deployment"], "service": a["target"]["service"],
            "replicas_desired": a["target"]["replicas_desired"], "pods": a["target"]["pods"],
            "containers": [{k: c[k] for k in ("name", "image", "requests", "limits", "readiness_probe", "liveness_probe")}
                           for c in a["target"]["containers"]],
        },
        "timeline": [{"time": _fmt(e["t"]), "kind": e["kind"], "event": e["text"], "evidence": e["evidence"]}
                     for e in a["timeline"]],
        "key_times": {k: _fmt(v) for k, v in a["key_times"].items()},
        "signals": {
            "traffic": {**a["traffic"], "onset_t": _fmt(a["traffic"]["onset_t"]), "peak_t": _fmt(a["traffic"]["peak_t"]),
                        "return_t": _fmt(a["traffic"]["return_t"])},
            "errors": {k: (_fmt(v) if k.endswith("_t") else v) for k, v in a["errors"].items()},
            "latency": {k: (_fmt(v) if k.endswith("_t") else v) for k, v in a["latency"].items()},
            "per_container_resources": res_summary,
        },
        "container_terminations": [{**t, "t": _fmt(t["t"])} for t in a["terminations"]],
        "hypotheses": a["hypotheses"],
        "rule_engine_confidence": a["confidence"],
        "impact": {k: (_fmt(v) if k.endswith("_t") else v) for k, v in a["impact"].items()},
        "severity": a["severity"],
        "changes_before_incident": {k: len(v) for k, v in a["changes"].items()},
        "kubernetes_events": [{**g, "first": _fmt(g["first"]), "last": _fmt(g["last"])} for g in a["k8s_events"]][:30],
        "log_groups": [{**g, "first": _fmt(g["first"]), "last": _fmt(g["last"])} for g in a["log_groups"]][:30],
        "last_logs_before_termination": a["last_logs_before_termination"],
        "evidence_catalog": a["evidence"],
        "evidence_source_status": [s for s in a["source_status"] if s["status"] != "ok"] or "all sources collected OK",
    }


def generate_narrative(bundle: dict, a: dict, settings, log=print) -> tuple[dict, str]:
    """Return (narrative, generator_label)."""
    if settings.llm_configured:
        if settings.llm_provider == "nvidia":
            attempts = [(m, lambda m=m: _nvidia(bundle, a, settings, log, m))
                        for m in (settings.nvidia_model, *settings.nvidia_fallback_models)]
        else:
            attempts = [(settings.anthropic_model, lambda: _claude(bundle, a, settings, log))]
        for model, call in attempts:
            try:
                out = call()
                _validate(out, a)
                return out, model
            except Exception as exc:  # noqa: BLE001 - try the next model, then the rule-based narrative
                log(f"  ! {model} failed ({type(exc).__name__}: {str(exc)[:200]})")
        log("  all LLM attempts failed; using rule-based narrative")
    else:
        log(f"  LLM disabled or no API key for provider '{settings.llm_provider}'; using rule-based narrative")
    return template_narrative(a), "rule-engine"


def _validate(out: dict, a: dict) -> None:
    """Check the model's JSON against NARRATIVE_SCHEMA's shape and drop unknown evidence IDs."""
    for key, spec in NARRATIVE_SCHEMA["properties"].items():
        if key not in out:
            raise ValueError(f"LLM output missing field '{key}'")
        expected = {"string": str, "array": list, "number": (int, float)}[spec["type"]]
        if not isinstance(out[key], expected):
            raise ValueError(f"LLM output field '{key}' has wrong type")
    known = {e["id"] for e in a["evidence"]}
    out["causal_chain"] = [s for s in out["causal_chain"] if isinstance(s, dict) and "step" in s]
    for step in out["causal_chain"]:
        step["evidence"] = [e for e in step.get("evidence", []) if e in known]
    out["ruled_out"] = [r for r in out["ruled_out"] if isinstance(r, dict) and "hypothesis" in r and "reason" in r]
    for key in ("contributing_factors", "recommended_next_steps"):
        out[key] = [str(x) for x in out[key]]
    out["confidence"] = max(0.0, min(1.0, float(out["confidence"])))


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a chat reply (tolerates <think> blocks and ``` fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start:end + 1])


def ping(settings) -> str:
    """Tiny request to verify the configured LLM key/model (used by `check` / `llm-test`)."""
    if not settings.llm_configured:
        return f"not configured (provider {settings.llm_provider})"
    if settings.llm_provider != "nvidia":
        return "anthropic key present (not tested)"
    import requests
    results = []
    for model in (settings.nvidia_model, *settings.nvidia_fallback_models):
        t0 = time.time()
        try:
            r = requests.post(settings.nvidia_base_url.rstrip("/") + "/chat/completions",
                              headers={"Authorization": f"Bearer {settings.nvidia_api_key}"},
                              json={"model": model, "max_tokens": 200,
                                    "messages": [{"role": "user", "content": "Reply with the word OK."}]},
                              timeout=45)
            status = f"OK ({time.time() - t0:.1f}s)" if r.status_code == 200 else f"HTTP {r.status_code} {r.text[:120]}"
        except requests.RequestException as exc:
            status = f"no response ({type(exc).__name__} after {time.time() - t0:.0f}s)"
        results.append(f"{model}: {status}")
    return "; ".join(results)


def _nvidia(bundle: dict, a: dict, settings, log, model: str) -> dict:
    """OpenAI-compatible chat completion against NVIDIA's hosted endpoint (build.nvidia.com)."""
    import requests

    payload = json.dumps(build_llm_input(bundle, a), default=str)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\nRespond with ONLY a single JSON object (no prose, no "
         "markdown) that conforms to this JSON Schema:\n" + json.dumps(NARRATIVE_SCHEMA)},
        {"role": "user", "content": "Investigate this incident. Structured evidence:\n\n" + payload},
    ]
    url = settings.nvidia_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.nvidia_api_key}", "Accept": "application/json"}
    body = {"model": model, "messages": messages, "temperature": 0.2, "top_p": 0.9,
            "max_tokens": 8192, "stream": False, "response_format": {"type": "json_object"}}
    log(f"  asking {model} (NVIDIA) to reason over {len(a['evidence'])} evidence items")
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=settings.llm_timeout_s)
        except requests.Timeout:
            raise RuntimeError(f"no response within {settings.llm_timeout_s:.0f}s (endpoint overloaded?)")
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")  # model doesn't support JSON mode; rely on the prompt instead
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last_err = f"HTTP {r.status_code}"
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 401 or r.status_code == 403:
            raise RuntimeError(f"NVIDIA API rejected the key (HTTP {r.status_code}); check NVIDIA_API_KEY")
        r.raise_for_status()
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("model reply truncated at max_tokens")
        try:
            return _extract_json(choice["message"].get("content") or "")
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = f"unparseable JSON ({exc})"
            messages = messages[:2] + [
                {"role": "assistant", "content": choice["message"].get("content") or ""},
                {"role": "user", "content": "That was not a valid JSON object matching the schema. "
                 "Reply again with only the JSON object."}]
            body["messages"] = messages
    raise RuntimeError(f"NVIDIA API call failed after retries: {last_err}")


def _claude(bundle: dict, a: dict, settings, log) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    payload = json.dumps(build_llm_input(bundle, a), default=str)
    request = dict(
        model=settings.anthropic_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": NARRATIVE_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "Investigate this incident. Structured evidence:\n\n" + payload}],
    )
    log(f"  asking {settings.anthropic_model} to reason over {len(a['evidence'])} evidence items")
    try:
        # Server-side fallback: if the model declines, the API re-routes to a fallback model.
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
    except anthropic.BadRequestError:
        resp = client.messages.create(**request)
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("response truncated at max_tokens")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def template_narrative(a: dict) -> dict:
    """Deterministic narrative from the analysis, used when the LLM is unavailable."""
    t, imp, tr, er = a["target"], a["impact"], a["traffic"], a["errors"]
    dep, entry = t["deployment"], a["entry_app"]
    top = a["hypotheses"][0]
    kt = a["key_times"]
    limits = next((c["limits"] for c in t["containers"]), {})
    peak_err = f"{(er['peak_ratio'] or 0) * 100:.0f}%"
    by_kind = {}
    for e in a["timeline"]:
        by_kind.setdefault(e["kind"], e)

    if top["id"] == "traffic_resource_exhaustion":
        summary = (f"The {dep} service in namespace {t['namespace']} suffered elevated failures (peak {peak_err} HTTP 5xx at {entry}) "
                   f"after incoming traffic rose from ~{tr['baseline_rps']:.0f} to ~{tr['peak_rps']:.0f} req/s.")
        root = (f"A sudden ~{tr.get('multiplier', 0):.1f}x traffic increase pushed {dep} pods past their resource limits "
                f"(CPU {limits.get('cpu', '?')}, memory {limits.get('memory', '?')}). Requests queued faster than the CPU-limited "
                f"workers could process them, memory grew with the backlog, and containers were "
                f"{'OOMKilled' if imp['oom_kills'] else 'restarted/marked unready'} ({imp['container_terminations']} terminations), "
                f"leaving the service with reduced or no ready endpoints and causing HTTP 5xx errors at {entry}.")
    else:
        summary = f"{dep} in namespace {t['namespace']} experienced elevated failures (peak {peak_err} HTTP 5xx at {entry})."
        root = f"Most likely explanation (rule engine): {top['title']}."
    chain = [{"step": e["text"], "evidence": e["evidence"]} for kind in
              ("traffic", "cpu", "memory", "termination", "availability", "errors", "recovery") if (e := by_kind.get(kind))]
    ruled = [{"hypothesis": h["title"],
              "reason": "; ".join(f"{'passed' if c['result'] else 'failed'}: {c['check']}" for c in h["checks"])}
             for h in a["hypotheses"][1:] if h["score"] < top["score"]]
    return {
        "title": f"{dep} service degradation" + (" after traffic spike" if tr["spike"] else ""),
        "summary": summary,
        "symptom": f"HTTP 5xx error rate at {entry} rose to {peak_err} and p95 latency reached "
                   f"{(a['latency']['peak_p95_s'] or 0) * 1000:.0f} ms.",
        "root_cause": root,
        "causal_chain": chain,
        "contributing_factors": [f"{dep} has no load shedding / request backlog limit",
                                 f"Fixed replica count ({t['replicas_desired']}) with no autoscaling"]
        if top["id"] == "traffic_resource_exhaustion" else [],
        "impact_summary": f"{imp['requests_failed']} of ~{imp['requests_total']} requests failed over {imp['duration_s']}s; "
                          f"{len(imp['affected_pods'])}/{imp['pods_total']} {dep} pods affected.",
        "ruled_out": ruled,
        "confidence": a["confidence"],
        "confidence_rationale": f"Rule-engine score {top['score']:.2f} for the top hypothesis; "
                                f"{sum(c['result'] for c in top['checks'])}/{len(top['checks'])} checks passed.",
        "recommended_next_steps": [],
    }
