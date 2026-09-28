# War Room stage 1 execution report — 2026-09-28

## Scope and release state
- Base: e96bbd84f7326e370cfe92d814468c43a4ec2c5c.
- Branch: fix/warroom-stage1-20260928; isolated worktree.
- Production deployment, service restart, merge and push: NOT performed.
- User representative completion: NOT performed.
- Existing production registry returned 200 records (API limit), including 3 RUNNING records. Their live-versus-stale status was not resolved; no restart was attempted.

## Implemented
- Task profiles: acknowledgement, read-only analysis, code change.
- Task-specific completion conditions instead of blanket full-regression/artifact requirements.
- Server response receipts passed to independent QA; a receipt proves response delivery, not task correctness.
- New-profile narrative claims reviewed by independent QA, not contradictory keyword vetoes. ACK tool use and read-only write guards remain.
- Worker 3600-second class on Simple FastGateway lane; separate bounded QA window.
- Explicit strict-evidence requests cannot be silently dropped; they remain on the advanced contract path.
- New profiles do not infer undeclared global session snapshot obligations from forbidden-action prose.
- Receipt tampering rechecked at final-approval readiness.
- Observer shutdown joins threads and rejects late resurrection.

## Verification
- regression-release.txt: 335 passed, 5 subtests passed, 7 existing deprecation warnings; 101.80 seconds.
- Actual ERPmanager -> FastGateway -> ERPqa: PASS in isolated task 196af42c-05a6-4f77-8f83-d7af26b519df.
- final_approval_missing: []; final approval itself intentionally not executed.
- Actual UAT service elapsed: 31.229 seconds.
- Browser: Chrome desktop 1365x900 and mobile 412x915, HTTP 200, QA PASS visible, final approval controls visible, no horizontal overflow or page errors.
- Browser verified rendering of actual isolated UAT state; task creation/initial test approval used the normal API, not browser clicks.

## Limits and remaining work
- No claim that the production War Room now uses this code: its service is still on the original checkout/version.
- General result-only revalidation, QA-only resume, and old-task migration are not implemented here.
- The separate Phase 2 multi-execution lane was not converted to the new time/contract path.
- Real-agent read-only analysis, code-change tasks and multi-worker stress remain unverified.
- An additional detailed execution-observation collection patch was blocked by the tool security check and was not applied or retried.
- JEV was not made a new mandatory gate and no new autonomous recovery permission was added.

## Issues encountered and disposition
- First test startup failed because the isolated checkout lacked an untracked static/vocal-coach directory. Created that empty directory only in the test checkout.
- An old UI asset-version assertion was updated to the new actual asset version.
- Transient database-lock failures occurred in an earlier run. Observer close had a reproducible late-controller resurrection race; fixed and added regression coverage. The database-lock root cause is not conclusively attributed to that race.
- Second actual-agent UAT reached QA PASS but final readiness required an undeclared SESSION_INTEGRITY snapshot. Corrected contract generation, added explicit-evidence preservation tests, and reran actual agents.
- Third actual-agent UAT reached QA PASS and empty final-readiness missing list.
- First browser attempt used the advanced /war-room route. Corrected the test URL to /war-room/simple; desktop/mobile checks then passed.
