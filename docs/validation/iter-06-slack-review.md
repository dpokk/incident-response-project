# Iteration 6 live validation: Slack incident thread + human review

Date: 2026-10-01. Cluster: minikube profile `incident-demo`, namespace `shop`. The code ran from the
`feature/iter-06-slack-human-review` worktree, using `python -m investigator watch`, which includes the Socket
Mode review listener. One Slack app was used (bot token, webhook and Socket Mode app token), with one configured
approver.

## Flow

| Time | Step | Result |
|---|---|---|
| 17:22:36 | `watch` started | Review listener connected (Socket Mode); 1 approver configured |
| ~17:22:40 | Failure A injected (`loadgenctl spike --peak 800`) | A traffic spike only; no Kubernetes object was edited |
| 17:23:09 | Detection | `INC-20261001-172302`; root message posted (the thread is created) |
| 17:24:11 | Investigation | Memory exhaustion in `backend`, 85% confidence; plan `action_proposed`: action 1 `adjust_resource_limit` (current 192Mi, `parameters_complete: false`), action 2 `investigate_further` |
| 17:24:11 | Plan registered | Digest `727589a971d1…` |
| 17:24:12 | Slack | Investigation and plan posted as replies in the incident thread |
| 17:25:26 | Approve action 1, no value | Refused (`parameter_missing`), private notice; not effective |
| 17:25:48 | Symptoms cleared | Root message updated |
| 17:26:14 | Approve action 1 with `128Mi` | Refused (`parameter_invalid`: must be higher than the current 192Mi) |
| 17:26:32 | Approve action 1 with `256Mi` | **Recorded**, effective; the plan message was re-rendered, the decision was posted in the thread and the root summary was updated |
| 17:26:47 | Acknowledge action 2 | **Recorded**, effective |

## Verified

- **Decision records** (`state/reviews.db`): four attempts, all bound to digest `727589a971d1…`, each with the
  action index and type, the reviewer's Slack ID, the time, `effective` and a reason. Only the `256Mi` approval
  (`{"proposed_limit": "256Mi"}`) and the acknowledgement are effective.
- **Digest binding:** the digest recomputed from the stored plan JSON, and from `reports/INC-…plan.json`,
  equals the registered digest.
- **The plan is unchanged by the review:** `approval.status: awaiting_review`, `execution.status: not_executed`,
  `executable: false`. A decision is a separate record; it never rewrites the plan.
- **Slack calls:** every `chat.postMessage`, `chat.update` and `response_url` call succeeded (no failures in
  the log). The bot lacks `channels:history`, so the thread contents were confirmed by the reviewer in Slack, not
  read back by the tool.
- **Nothing executed.** Compared with the snapshot taken before the review:
  - every Deployment, ConfigMap, Service and Secret in `shop` kept its `generation` and spec;
  - `backend` still has a 192Mi limit and 2 replicas.

  The only `resourceVersion` change was on `backend`. Its managed fields show that the last write came from
  `kube-controller-manager` on the `status` subresource at 17:24:56, before any click. The last spec write was
  from an earlier session.

## Found and fixed during validation

- **Refused attempts did not keep the typed value.** The `128Mi` attempt was audited with empty parameters.
  Refused attempts now store `{"raw_input": …}`. The value is unvalidated, never used and never effective.
  A test was extended to cover this.

## Not exercised live (covered by offline tests)

- **Conflicting click.** Once an action is decided its buttons are replaced by the decision, so a conflicting
  click is not possible from the UI.
- **Clicks from a non-approver.**
- **Supersession by a newer plan.**

## Observed, outside Iteration 6

- **A second incident was detected.** About 2 minutes after recovery, `watch` opened `INC-20261001-172825` from
  175 frontend error lines (after-effects of the spike), and its detection root message was posted. The listener
  was stopped before it was investigated. This is existing detection sensitivity, not part of the review flow.
