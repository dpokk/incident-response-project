# Iteration 4 validation: OOM investigated after the failing instances are gone

Run on 2026-10-01 against the local cluster (minikube profile `incident-demo`), on branch
`feature/iter-04-oom-postmortem-validation`.

## Procedure

1. Start the evidence recorder: `python -m investigator record`. It started at 12:06:41, so the 11 seconds
   before that are a stated coverage gap.
2. Record a 60-second healthy baseline.
3. Run `scripts\inject\oom-then-replace.ps1`:
   - 12:07:37: traffic spike to 800 req/s;
   - 12:07:44: both backend pods are OOM-killed;
   - 12:07:48: the simulated operator stops the spike and replaces the backend pods with a rollout restart.
     The OOM-killed pods, and Kubernetes' copy of their logs, are gone.
4. Let the system recover for 90 seconds.
5. Investigate the window 12:06:30–12:10:19 twice, giving the investigator no failure information:
   - with evidence history;
   - with `HISTORY_ENABLED=false`, which shows only what the live system still had.

## Result

| | Diagnosis | Confidence | Evidence it rests on |
|---|---|---|---|
| Without history | **Undetermined** | 0% | The live state shows two healthy new pods and nothing about an OOM. |
| With history | **Memory exhaustion in backend** | **85% (High)** | Retained OOMKilled terminations of both deleted pods. Their retained logs, which end with "memory usage approaching container limit" (12:07:44). Repeated kills. |

The confidence rose only because more evidence existed. The diagnosis engine is the same in both runs.

### The rest of the report, with history

- **Impact:**
  - backend: 2 killed instances, both no longer existing;
  - postgres: healthy, with evidence;
  - frontend: impacted;
  - users: about 4,070 of 5,702 requests failed (an estimate from sampled rate × error ratio);
  - duration: about 31 s; propagated.
- **Timeline:**
  - user errors began between 12:07:25 and 12:07:45;
  - first kill at 12:07:44, the order of the two undetermined;
  - recovery at 12:07:56;
  - the operator's rollout is listed as a change *after* the failure;
  - the coverage gap is stated.
- **Correlation:** traffic rose 4.7x, but whether before or after the first kill cannot be told. The kill
  came 7 s into a ramp measured as a 15 s rate, so traffic is **not** claimed as a cause.
- **Memory metrics:** the highest sample was 47% of the limit, with about one sample every 38 s. This is
  stated as a sampling limit that neither confirms nor contradicts the kills.

## What the validation found and fixed

- **The recorder's pod watch delivered events about 60 s late** with this environment's HTTP client. An
  ended run's logs were then fetched after the pod was already deleted. The recorder now detects lifecycle
  changes by comparing pod snapshots every poll (5 s) and fetches ended runs' logs in the same poll.
  Regression tests are in `tests/test_recorder.py`.

## Limits

- **The memory warning is not guaranteed.**
  - The backend logs it only on a 2-second stats tick at 80% of the limit or more.
  - An earlier attempt the same morning was killed about 6 s into the spike without logging it. Retained logs
    then add only the backlog warnings, and the diagnosis would rest on the retained kills: 70%, still memory
    exhaustion, not "Undetermined".
- **No recording, no history.** Nothing is retained while neither `record` nor `watch` is running.
- **One live run is a demonstration, not a statistic.** The offline regression is
  `test_oom_after_the_pods_were_replaced_needs_retained_history`.

# Finalization pass (2026-10-01): further live validations

## OOM crash-loop: the Iteration 3 problem

Run `scripts\inject\oom.ps1` with the pods left crash-looping (3–4 restarts each), then investigate the same
window both ways.

| | Kills seen | Memory warning | Result |
|---|---|---|---|
| Without history | 2 (latest per pod) | none: Kubernetes serves only the 6 s run | memory exhaustion, 70% |
| With history | 8 (every kill) | retained, from `tmxfk` run #1, the 587 s run that took the spike | memory exhaustion, 85% |

The code and the window are the same in both runs. The +15 points are solely the retained memory-warning
fact.

## PostgreSQL down, recovered before the investigation

Procedure:
1. Start the recorder.
2. 12:46:22: `db-down.ps1`.
3. 12:47:37: scale postgres back to 1; it is ready at 12:47:45.
4. Before investigating, confirm PostgreSQL is healthy: a new pod, 1 ready endpoint, and `pg_isready`
   "accepting connections".
5. Investigate 12:45:30–12:49:13 with no failure information, once with and once without history.

**Without history: application crash in backend, 75%. Wrong.** One backend instance exited at 12:47:46, 1 s
after PostgreSQL came back. Its logs end with an unhandled `_queue.Empty` after a
`psycopg.errors.UniqueViolation`. This is the demo app's reconnect bug, the "unexplained exit" open since
Iteration 3. With PostgreSQL healthy now, nothing pointed at it.

**With history: dependency unavailable.** Root cause postgres, affected backend, impacted frontend.
- **The planner's decision:** "read availability history of postgres:5432 — backend logged errors about it,
  but it looks healthy now".
- **Evidence:**
  - backend logged 7,848 connection errors for postgres:5432;
  - recorded history: service postgres had no ready endpoints from 12:46:22–12:46:28 until 12:47:45–12:47:51,
    and postgres was scaled to 0 replicas; both bounded by two recorder polls;
  - "It has recovered since": 1 ready endpoint, and a TCP connection succeeds now.
- **Timeline:**
  - onset 12:46:22;
  - the outage from 12:46:22–12:46:28;
  - the backend crash at 12:47:46;
  - all instances ready again by 12:47:51;
  - duration 1m 28s.
- **Impact:**
  - postgres "unavailable during the incident; healthy at investigation";
  - frontend impacted;
  - users: the probe succeeded *at investigation time*, and user impact during the incident was not
    measured.
- **Confidence: 57% (Medium).** The backend crash during recovery is a real second finding and the
  runner-up. Recorded history now stops the crash check from claiming the dependency was "reachable",
  because it was down until 1 s before the exit.