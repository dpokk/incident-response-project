# Iteration 7: Demo A load calibration (experiment record)

Date: 2026-10-01. Cluster: minikube `incident-demo`. Backend: 2 replicas, 500m CPU, 192Mi limit, a 256 KB buffer
per in-flight request, readiness failing above 300 in flight. Frontend upstream timeout: 3 s.

**Question.** Is there a sustained load at which the backend repeatedly OOMs at 192Mi, but stays healthy through a
verification window at 512Mi? The same load would then be used, with an insufficient limit, for the failed-remediation
demo.

## Method

The load generator was set to a sustained baseline rate. Every 5 s the backend pods were sampled: readiness,
restarts, in-flight requests, cgroup memory, and kills at the memory limit after the measurement started. User
outcomes came from the load generator's 10 s counters.

For the "cascade" runs:
1. A 20 s spike to 400 rps was added on top of the sustained rate.
2. The runs waited until no replica was ready and at least one was in restart back-off.
3. The limit was then changed by a direct Deployment patch: a rollout under the same load, as the executor would do.
4. The backend was measured for 240 s.

Between runs the limit was restored to 192Mi and the load to 100 rps, and the next run started from a healthy
state. The change made in each run was temporary.

## Results

| Run | Limit / load | OOM kills after start | Not-ready samples | User failures | Peak memory | Notes |
|---|---|---|---|---|---|---|
| step 1 | 192Mi, 200 rps (healthy start) | 6 | 14/24 | 62.5% | 177 MB | backlog grows ~25/s per pod: above capacity |
| E1 | 192Mi, 130 rps (healthy start) | 0 | 0/48 | 0% | 40 MB | stable |
| E1b | 192Mi, 170 rps | 0 | 0/36 | 0% | 40 MB | stable |
| E1c | 192Mi, 185 rps | 0 | 0/36 | 0% | 40 MB | stable |
| E1d | 192Mi, 200 rps (repeat) | OOM at the end | 4/24 | 22.9% | 169 MB | backlog grows ~9/s per pod: capacity ≈ 190 rps |
| E2 | 192Mi, cascade, then 150 rps, no change | (restarts 4) | 94/96 | 98.8% | 172 MB | cascade persists |
| E3 | cascade @150, then 512Mi | 0 | 2/96 | 15.7% (incl. rollout) | 114 MB | recovered; the peak would also fit 192Mi |
| E4 | cascade @150, then 200Mi | — | 4/96 | 14.4% | 158 MB | recovered (the cascade was not fully re-established before the change) |
| E5 | cascade @170, no change (192Mi) | **7** | 86/96 | **95.1%** | 184.5 MB (96% of limit) | cascade persists |
| E6 | cascade @170, then **512Mi** | **0** | 9/96 | 23.5% (incl. rollout) | **202.9 MB** | recovered; 0% failures from ~85 s after the change |
| E7 | cascade @170, then **200Mi** | **1** | 5/96 | 14.4% | 176.5 MB | recovered after one more kill |

## Findings

1. **At a constant rate, the limit makes no difference.**
   - Below capacity (≈190 rps for 2 replicas) there is no OOM at any limit (E1–E1c).
   - Above capacity the backlog grows without bound, so a larger limit only delays the kill (step 1, E1d), as the
     planner's risk statement for this action says.
2. **After an OOM cascade, a sustained near-capacity load keeps 192Mi failing.** Without a change the backend keeps
   being killed (E2, E5: 7 kills, 95% failures in 4 min).
3. **Any rollout under that load ended the cascade, including the insufficient 200Mi control** (E4 at 150, E7 at
   170: one further kill, then stable). The rollout restarts both replicas together, escaping the staggered restart
   back-off. That is the dominant recovery mechanism, not the extra memory.
4. **At 170 rps, memory headroom did matter during the rollout.**
   - 512Mi absorbed a 203 MB peak with no kill (E6), where 200Mi was killed once (E7).
   - The outcome over the window was nevertheless the same.
5. **Recovery under sustained load takes time.** With 512Mi at 170 rps, failures continued intermittently for
   ~75–85 s after the change while readiness flapped; then 0% failures for the remaining 2.5 min (E6).

**Conclusion.** No sustained load was found at which 192Mi repeatedly OOMs and 512Mi stays healthy *because of the
larger limit*, while an insufficient limit does not. Building Demo A and the failed-remediation demo on this load as
originally planned would misattribute the result. The verifier itself reports observed state, not cause, so it would
not be wrong, but the demo narrative would be.

The cluster was restored after the experiment: 192Mi, 2 replicas, 100 rps, all pods ready.
