# War Room full-flow remediation — NOT RELEASE READY

Date: 2026-09-28
Base production commit: cbeb70f6df491ce7f3b80e91d6fdcc364dfcfc09
Working branch: fix/warroom-flow-completion-20260928
Production was NOT merged, changed, restarted, or redeployed in this turn.

## Final result
Final regression: 343 passed, 2 failed, 5 subtests passed, 7 deprecation warnings, 74.25 seconds. Exit code 1.
New fault-injection tests: 8 passed, 2 failed. Test boundaries are isolated databases and simulated external agents, not a real-agent end-to-end acceptance test.

## New fault-injection checks which passed
- Partial multi-worker registration rolls back instead of leaving a partial workflow.
- Conflicting retry cannot overwrite the saved original input, including after orchestrator restart.
- A slow worker does not serialize an unrelated worker; a late PASS does not overwrite cancellation.
- A transient observation timeout preserves RUNNING and does not abort the worker.
- A slow JEV observation does not block dispatch or status access.
- A late failure does not change a stopped task.
- A QA delivery uses the direct adapter when stopped, even when its parent uses FastGateway.
- An expired queued task is not kept pending forever by another busy task.

## Implemented but not accepted as complete
- QA-only resume API and UI, stage-specific issues and immutable attempt snapshots.
- New-profile result problems retain the task revision and sibling outputs instead of escalating immediately to whole-task rework. Legacy contracts retain their prior behavior.
- Independent observation retry/error logging and asynchronous JEV assessment.
- Polling a terminal result before processing an expired delivery; remaining active runs require an observed stop result.
- Phase-2 War Room runtime class selection and exact artifact path forwarding.
- Stale-revision error filtering and stage-specific error display.

## Blocking failures
Two integration cases did not reach QA PASS after requesting QA-only resume:
1. Reviewer-session provisioning failure.
2. Malformed QA response.
The test endpoint was reached and did not redispatch the Worker, but a final QA verdict was absent. This is a failed acceptance result, not a pass. The suspected test/runtime adapter wiring issue is not confirmed as fixed.

## Tool-security blocked operations — not applied and not retried via alternate routes
- Same-output revalidation implementation using the original bound session history.
- A grouped edit for dependency-graph cancellation, dispatch-error reconciliation, and durable stop intent.
- A subsequent test-fixture adapter-wiring correction.

No replacement of rejected evidence, approval bypass, bulk deletion of old test records, or representative final approval was performed.

## Remaining acceptance work
Same-result revalidation; complete dependency-graph/project stop; QA-only resume passing; existing-contract migration; real-agent read-only/code-change/multi-worker end-to-end scenarios. This checkpoint is intentionally not a production release.

## Evidence
- final-regression.txt
- fault-injection-expanded.txt
- first-regression.txt
- tests/test_war_room_flow_completion.py
