# Canonical State

Last checked: 2026-09-06 (Asia/Bangkok). This is the current canonical record. Historical or superseded material belongs in `90-HISTORY.md`.

## Source of Truth

The Ubuntu LIVE working tree is the primary Source of Truth:

- Repository: `/home/plachem-sever/codex-workspaces/cha-secretary/plachem-ai-server-monitor`
- Branch: `main`
- LIVE HEAD observed: `6864ddb70ee97ae137dbdb02b159a6fb957a6d25`
- Preserve ref: `refs/heads/preserve/live-head-20260906T010242Z` at the same HEAD
- GitHub is secondary evidence until LIVE and GitHub are explicitly synchronized and that synchronization is verified.
- The LIVE worktree is dirty; existing user changes are preserved.

## Current state

- Main Direct Coding: `0` for this documentation task.
- Execution authority: Fast Gateway/Core for `FAST_GATEWAY`; War Room is control, approval, readiness, observation, projection, and stop control.
- Placement: `LOCAL-FIRST`; CLOUD is only a minimal, explicit escalation.
- Modes: `FAST_GATEWAY` and `LEGACY` are explicit. No silent fallback or provider substitution.
- Recovery: `RECOVERY_IN_PROGRESS`; no recovery completion is claimed.
- Current task: correct these seven documents only. No runtime, service, source, refs, recovery worktree, database, or cleanup work is authorized.

## Roles

- User: supplies intent and approval boundaries.
- Main/Supervisor: coordinates, reconciles evidence, and reports; does not directly code or bypass execution authority.
- War Room: approval/scope, dependency/readiness, observation, projection, and stop control.
- Command Center/Fast Gateway: admission, routing, authorization, lifecycle, timeout, cancellation, validation, and result finalization.
- Workflow/Dependency/Readiness: compiles prerequisites and prevents premature or duplicate dispatch.
- Persistent Execution Harness: preserves bounded run ownership, terminal handling, and evidence linkage; it is not a second execution authority or a core database.
- OpenClaw: executes the selected agent session through the verified transport contract.
- LOCAL-first Agent: performs work under the registered LOCAL profile; CLOUD escalation is explicit only.
- OpenConnector: credential/OAuth/connection store and provider connector boundary; it is not task execution authority and not the core execution database.
- ERPmanager/ERPcoder/other agents: workers selected by registry and assigned task scope; they do not redefine authority.

## Concise VERIFIED list

- VERIFIED: LIVE repository, `main`, observed HEAD, and preserve ref above.
- VERIFIED: this task is documentation-only and limited to the seven files in `docs/openclaw-project/`.
- VERIFIED: current recovery label is `RECOVERY_IN_PROGRESS`; completion is not claimed.
- VERIFIED: known bad-chain candidates have no deletion/disposition proof and remain `UNVERIFIED / NEEDS_CONFIRMATION`.
- VERIFIED: binding means run/session ownership and cancellation lineage; it is distinct from the core execution database and from OpenConnector.

## Recovery, current task, known bad, and DO NOT

- Recovery status: `RECOVERY_IN_PROGRESS`; no recovery implementation is being performed here.
- Current task: maintain these seven canonical documents only.
- Known bad chain: `87572f34d2f175a75e94fb83453b94560385088a` → `9f76d4b3577e7816da02d6d6ba8a2b5b1b37fc6a`. The three associated bad-only candidates remain `UNVERIFIED / NEEDS_CONFIRMATION`.
- DO NOT delete, reset, revert, clean, rewrite, repair, or reinterpret candidates, refs, runtime data, recovery worktrees, services, or databases without separate explicit approval and evidence.

## VERIFIED list discipline

Use `VERIFIED` only when the exact target, date, revision, command/result or UI/DB evidence, and scope are named. Historical PASS is not current LIVE verification. Otherwise use `UNVERIFIED` or `NEEDS_CONFIRMATION`.

## Documentation scope

Only the seven Markdown files in `docs/openclaw-project/` are in scope. Functional tests, runtime inspection, SQLite inspection/write, service changes, source changes, Git ref changes, commit, push, deploy, and cleanup are out of scope.
