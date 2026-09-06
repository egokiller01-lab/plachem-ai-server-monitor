# Architecture

## Canonical execution flow

```text
User / War Room
  → Task Intent
  → Command Center / Fast Gateway
  → Workflow / Dependency / Readiness
  → Persistent Execution Harness
  → OpenClaw
  → LOCAL-first Agent
  → Result / Evidence
  → Parent Finalization
```

This is one authority chain. War Room authorizes, compiles, observes, projects, and requests stop. Command Center/Fast Gateway is the sole `FAST_GATEWAY` execution authority. The Persistent Execution Harness supplies run binding and bounded terminal/evidence handling inside that chain; it is not a separate authority and not the core execution database. OpenClaw is the transport/runtime boundary. Parent finalization consumes validated result/evidence and closes the dependency graph.

## Roles and boundaries

- User: provides task intent and explicit approvals.
- Main/Supervisor: coordinates and reports; no direct coding or manual bypass.
- War Room: approval, scope, workflow compilation, dependency/readiness, observation, result projection, and stop control.
- Task Intent: immutable goal, scope, mode, and acceptance boundary.
- Command Center/Fast Gateway: owns admission, run identity, routing, policy, budgets, timeout, cancellation, validation, and finalization for `FAST_GATEWAY`.
- Workflow/Dependency/Readiness: checks prerequisites, ordering, duplicate dispatch, and parent-child readiness.
- Persistent Execution Harness: binds the owning run/session, bounds execution/finalization, records terminal state, and links evidence; it does not create an independent run truth or database authority.
- OpenClaw: performs the selected session through the verified transport contract.
- LOCAL-first Agent: executes under the server registry/profile; CLOUD is explicit escalation only.
- Result/Evidence: validated output with provenance, scope, status, and limitations.
- Parent Finalization: records the child outcome, updates dependency state, and reports only evidence-supported status.
- OpenConnector: credential/OAuth/connection/provider connector boundary; separate from task authorization, run binding, and the core execution database.
- Legacy: explicitly selected compatibility mode; never an implicit Fast Gateway fallback.

## Invariants

1. No caller-selected model/provider/endpoint/credential/workspace/session identity.
2. Caller timeout and wait values cannot override server policy.
3. `FAST_GATEWAY` and `LEGACY` remain visible, explicit modes.
4. Missing evidence, invalid result, loop suspicion, drift, or exhausted recovery is a guarded failure, not success.
5. Secrets and raw credentials are never persisted in prompts, browser state, audit payloads, or reports.
6. Current dirty LIVE verification remains `UNVERIFIED` unless exact evidence closes it.

Relevant implementation references are evidence only; this architecture document does not certify the current dirty tree.
