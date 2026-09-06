# Project Control

## Constitution — 15 operating rules

1. Main manual orchestration: `NO`; Main/Supervisor coordinates and reports but does not manually dispatch workers, select models, or judge run state.
2. Execution placement: `LOCAL-FIRST`; LOCAL is the default path.
3. CLOUD direct selection: `NO`; callers and Main cannot directly select CLOUD.
4. CLOUD use: minimal, explicit escalation only, with policy and evidence.
5. Existing LIVE/ref source first: inspect the existing LIVE working tree and its refs before relying on GitHub or historical material.
6. Test and Development are separated: test evidence cannot be presented as development or production acceptance.
7. Fast Gateway/Core is the sole execution authority for `FAST_GATEWAY` runs.
8. War Room is a control/approval/observation layer, not a second execution authority.
9. Callers cannot force model, provider, endpoint, credential, workspace, or session selection.
10. Caller timeout, wait, and budget values cannot override server policy or registered profile limits.
11. `FAST_GATEWAY` and `LEGACY` are explicit modes; silent Legacy fallback is prohibited.
12. Recovery belongs in the Persistent Execution Harness, with bounded ownership, terminal handling, cancellation, and evidence lineage.
13. Result/Evidence Truth validation cannot be bypassed; missing or invalid evidence is failure or unverified.
14. No new architecture is introduced before a documented GAP and explicit approval.
15. Scope Drift is prohibited; target, scope, mode, owner, and acceptance boundary remain fixed unless explicitly re-approved.

## Future-instruction conflict precheck

Before accepting any future instruction, precheck it against all 15 rules; confirm whether it changes the Source of Truth, introduces a second authority or silent fallback, expands into source/runtime/service/SQLite/database/refs/commit/push/deploy/cleanup, or lacks separate approval for the expanded action. Stop and report the conflict rather than silently reconciling it.

## Required handoff fields

Exact repo/worktree, branch and revision, task intent, mode, authority, roles, changed files, command/result evidence, runtime/UI/DB evidence where applicable, known failures, rollback boundary, current state label, and next approval gate.

## Current control state

`RECOVERY_IN_PROGRESS`; known bad-only candidate disposition is `UNVERIFIED / NEEDS_CONFIRMATION`. This correction authorizes no deletion, reset, revert, cleanup, runtime, service, source, database, commit, push, or deploy.
