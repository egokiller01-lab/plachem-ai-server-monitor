# Full-flow continuation — 2026-09-28

## Product change in this continuation
- Base checkpoint: 75f4788162fc38dab4e70de7c94fd42d80b9362d.
- Task mutation fingerprints now depend on the selected task, its approvals, deliveries, evidence, QA and project control rather than unrelated project-wide audit traffic.
- Signature, principal, action, target, expiry and last-moment authorization checks remain.
- Target-context suite: 7 passed, including rejection of target scope/revision changes and project stop.
- Complete selected regression suite: 347 passed, 2 failed, 5 subtests passed, 7 warnings, 109.56 seconds.

## Real-agent tests (isolated database; no production task approval)
| Scenario | Task | Outcome | Service duration |
|---|---|---|---|
| Read-only receivables analysis | 3377c16e-c478-4413-be73-faaf40209e47 | QA PASS; final checks missing=[] | 54.964s |
| Isolated ledger.py correction and tests | d0c0fe74-db32-4cb2-92e4-90d7ae97d677 | QA PASS; final checks missing=[] | 77.100s |
| Injected QA session creation fault | 0c473693-5d00-4bf8-a2d6-29b612e27a39 | QA-only resume, revision=1, cycle=2, worker_redispatched=false; QA PASS | 55.106s |
| Injected malformed QA response | 8b1eea9f-7f9c-4629-89ef-ec920963753c | QA-only resume, revision=1, cycle=2, worker_redispatched=false; QA PASS | 83.079s |
| ERPmanager + Qwentest, independent ERPqa | 51e802e7-4165-4f91-9a17-480e5d777672 | QA PASS then isolated approval simulation -> completed, HTTP 200 | 95.541s |

The final approval simulation used the isolated test identity/database, not the user's production representative approval. Tests exercised normal APIs and actual agents, not browser clicks.

## Two automated failures remain
The provision/format cases in test_war_room_flow_completion.py still fail. Source inspection shows they create a local runtime with RepairableReviewer, while resume_task_qa resolves a separate application runtime through _adapter(). The production fixture sets the real adapter to 0. These tests were not skipped, relabeled PASS or edited here. The actual-runtime fault tests above passed; that does not turn the failing automated tests into passes.

## Blocked operations and limits
- An interactive pytest debugger request was blocked by the tool security check.
- A proposed durable QA-resume queue rewrite was blocked before execution; not applied or rerouted.
- A proposed persistence change for validation context across restart was blocked before execution; not applied or rerouted.
- A combined live-result aggregation and independent rerun command was blocked. The report uses observed test process outputs, not an invented independent recheck.
- Result-only revalidation, legacy-task migration, restart restoration and full dependency-graph stop/resume acceptance are still incomplete.
- Parallel-worker success does not prove every dependency-graph failure/recovery case.
- Browser end-to-end interaction was not run in this continuation.

## Deployment
Production remains cbeb70f6df491ce7f3b80e91d6fdcc364dfcfc09. No merge, push or production service restart was performed. The WIP branch is not approved as a complete process repair.
