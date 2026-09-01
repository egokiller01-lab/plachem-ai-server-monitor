# War Room–Command Center Integration Contract

**Phase:** 2 #8
**Status:** Contract only — no integration implementation
**Command Center source:** `E:\PLACHEM-Agent-Control\repo`, branch `phase2-worker-identity`, commit `0aeeed5f2ab95935c50208b7d58a47eb257fa30e`
**War Room source:** `egokiller01-lab/plachem-ai-server-monitor`, branch `main`, commit `68467634412677858e3b42f86b5f4707d69b234c`
**War Room database:** SQLite at `~/.openclaw/war-room/war_room.sqlite3` by default
**Contract rule:** Command Center is the single source of truth for execution. War Room is the human/project collaboration layer.

---

## 1. Current War Room Architecture

The existing War Room is retained. It is not replaced or reimplemented.

### 1.1 Read and presentation layer

- `war_room.py` provisions and reads the SQLite collaboration model.
- It owns project, participant, message, timeline, project-session presentation, redaction, and read-only OpenClaw session summaries.
- `static/war-room.html` and `static/war-room-ui.js` present projects, tasks, participants, deliveries, timeline, evidence, audit, approvals, QA, and stop/resume controls.
- `tests/test_war_room.py` verifies read isolation, redaction, project scoping, cursor behavior, and read-only handling of OpenClaw session indexes.

### 1.2 Collaboration and mutation layer

- `war_room_actions.py` owns the current SQLite mutation API for projects, participants, messages, tasks, approvals, representative approvals, QA verdicts, evidence, audit, and project-level workflow.
- It currently creates `war_tasks`, assigns `war_task_agents`, marks tasks `running`, creates `war_deliveries`, tracks `war_task_calls`, and changes task state through War Room-specific states such as `draft`, `awaiting_approval`, `running`, `qa`, `completed`, `rework_required`, and `stopped`.
- These collaboration states may remain for human workflow, but they must not be interpreted as authoritative Worker execution states after integration.

### 1.3 Legacy execution layer

- `war_room_worker.py` currently claims queued deliveries, calls a session adapter, persists delivery status/run IDs, validates response bodies, updates task call counters, and recovers received deliveries after restart.
- `war_room_runtime.py` owns a long-lived adapter instance and can process deliveries, recover runs, and request project stops.
- `war_room_adapter.py` contains test adapters plus `PersistentGatewayBridge` and `OpenClawSessionAdapter`.
- `war_room_gateway_bridge.mjs` exposes an allowlist including `sessions.create`, `sessions.resolve`, `chat.history`, `chat.send`, `agent.wait`, and `chat.abort` through the OpenClaw Gateway client.
- `war_room_session_integrity.py` snapshots and compares existing business session state.
- `tests/test_war_room_actions.py` verifies idempotency, approvals, QA, evidence, delivery recovery, run ownership, exact stop/abort ownership, disposable session isolation, and explicit non-replay behavior.

This execution layer is classified as **Legacy/OpenClaw compatibility**, not the target execution architecture.

---

## 2. Current Command Center Architecture

| Component | Current responsibility |
|---|---|
| `task_intake.py` | Generates a Command task package from the original instruction, Worker request, and requested actions. |
| `agent_registry.py` | Resolves the configured Worker/provider and rejects unknown agents. |
| `workspace_registry.py` | Resolves an official workspace and validates canonical path, active status, and Git branch. |
| `task_dispatch.py` | Enforces package integrity, workspace guard, Agent Registry lookup, Task Auth Broker authorization, then invokes Fast Gateway. |
| `workflow_coordinator.py` | Runs tasks sequentially; creates the next task only after the prior task passes and stops on FAIL/BLOCKED. |
| `run_registry.py` | Append-only JSONL lifecycle state: `CREATED → DISPATCHING → RUNNING → PASS|FAIL|BLOCKED`. |
| `run_query.py` | Read-only latest/recent/active/terminal/Worker/count summary over Run Registry JSONL. |
| `result_evidence.py` | Normalizes Gateway result/evidence for presentation without re-evaluating it. |
| Fast Gateway | Executes the selected Worker under policy/authorization and produces the authoritative Gateway result. |

### 2.1 Current limitations relevant to integration

- `task_intake.py` does not yet carry `project_id`, external War Room IDs, or `correlation_id`.
- Run Registry currently uses `task_id` as its lookup key and has no separate explicit `run_id` field.
- Run Registry and Run Query do not yet persist or expose `correlation_id` or external reference IDs.
- Coordinator accepts one requested Worker per Command task. War Room fan-out must therefore compile to multiple independent Command tasks, not one shared execution record.
- Command Center has no pause/resume/cancel/project-stop API. War Room project control cannot be translated into execution control yet.

These are Phase 2 #9 candidates, not changes authorized by this contract.

---

## 3. Execution Source of Truth

### 3.1 Hard invariant

**Command Center Run Registry is the only source of truth for Worker execution lifecycle.**

Command Center exclusively owns:

- Command task execution
- future `command_run_id`
- lifecycle/status (`CREATED`, `DISPATCHING`, `RUNNING`, `PASS`, `FAIL`, `BLOCKED`)
- Worker identity and routing
- Agent Registry
- Workspace Registry and path/branch guard
- Coordinator ordering and multi-Worker sequencing
- Worker busy/control state when implemented
- Gateway execution
- execution Result/Evidence
- Run Query/Summary

War Room must not independently create, advance, recover, retry, stop, or finalize these execution states.

### 3.2 Projection rule

War Room may present an execution state only as a projection read from Command Center by immutable reference. It may not infer PASS from a delivery response, infer RUNNING from a queued message, or override a terminal Command Center result.

A War Room collaboration state and a Command execution state are separate dimensions. For example:

```text
War Room task workflow: awaiting_approval → in_review → qa → accepted
Command execution:       CREATED → DISPATCHING → RUNNING → PASS
```

The names need not match. No direct status equivalence is implied.

---

## 4. Responsibility Matrix

| Capability | Command Center | War Room |
|---|---|---|
| Human instruction/message | Receives normalized execution instruction | **Owner** |
| Project and participant membership | References only | **Owner** |
| Human/representative approval | Receives approved intent when required | **Owner** |
| Task execution authorization | **Owner** through Task Auth Broker/Gateway | Must not substitute human approval for execution authorization |
| Workspace selection/path/branch | **Owner** | Supplies only registered workspace ID |
| Worker identity and route | **Owner** | May request; cannot decide final route |
| Multi-Worker order | **Owner** through Coordinator | May express collaboration intent |
| Worker invocation | **Owner** through Gateway | Forbidden |
| Run lifecycle | **Owner** | Read-only projection/reference |
| Result/Evidence execution record | **Owner** | Presents immutable reference/content |
| QA verdict and evidence presentation | Supplies execution evidence | **Owner** |
| Timeline/audit presentation | Supplies Run events/references | **Owner** |
| OpenClaw session compatibility | No dependency in target path | Retained only behind Legacy adapter |
| Retry/pause/resume/cancel/stop | Command Center only when separately implemented | May request; cannot execute directly |

---

## 5. Data Ownership Matrix

| State/table | Classification | Owner after integration | Contract |
|---|---|---|---|
| `war_projects` | **KEEP** | War Room | Human/project collaboration root. |
| `war_participants` | **KEEP** | War Room | Human and project participant membership/permissions. |
| `war_messages` | **KEEP** | War Room | Human instructions, responses, and timeline messages. |
| `war_approvals` | **KEEP** | War Room | Human approval record; not Task Auth Broker authorization. |
| `war_representative_approvals` | **KEEP** | War Room | Representative human workflow decision. |
| `war_qa_verdicts` | **KEEP** | War Room | Human/project QA verdict over referenced execution evidence. |
| `war_audit_events` | **KEEP** | War Room | Collaboration/audit presentation; does not replace Command audit. |
| `war_evidence` | **REFERENCE** | War Room presentation; Command Center execution source | Human evidence may remain; execution evidence must be referenced, not independently re-created as truth. |
| `war_tasks` | **ADAPTER** | War Room collaboration workflow | Maps to one or more Command task IDs. Its status is not Worker execution status. |
| `war_deliveries` | **DEPRECATE** | Command Center Run Registry replaces execution semantics | Retain read compatibility during migration; no new authoritative delivery lifecycle after cutover. |
| `war_task_calls` | **DEPRECATE** | Command Center Run/Gateway evidence | Call/turn counters must come from authoritative execution evidence. |
| `war_task_agents` | **REFERENCE** | War Room intent; Command Center routing authority | Stores requested collaboration targets only. Agent Registry/Coordinator make the execution decision. |
| `war_project_sessions` | **ADAPTER** | Legacy/OpenClaw compatibility | Session/channel reference only; never Worker identity or Run ownership. |
| `war_project_control` | **ADAPTER** | War Room owns human project intent; Command Center owns execution control | Stop/resume requests require a future Command Center control contract. Direct adapter stop/abort remains legacy until then. |

### 5.1 Required duplicate-state treatment

1. `war_tasks` vs Task Intake: keep War task as project workflow; create immutable references to Command task IDs.
2. `war_deliveries` vs Run Registry: Run Registry wins; delivery becomes compatibility/reference data only.
3. `war_project_sessions` vs Agent Registry/Worker Identity: session binding is transport compatibility, not identity.
4. `war_task_agents` vs Coordinator routing: War Room expresses targets; Coordinator owns actual order and route.
5. `war_evidence` vs Result/Evidence: War Room presents; Command Center owns execution evidence.
6. `war_task_calls` vs Run execution counters: Command execution evidence wins; War counter is deprecated.
7. `war_project_control` vs Gateway execution control: War Room records human intent; only Command Center may perform control when that API exists.

---

## 6. ID Mapping

| ID | Owner / generator | Mutable? | Lifetime | Foreign reference | Restart persistence |
|---|---|---:|---|---|---:|
| `war_project_id` | War Room, generated by War Room project workflow | No | Project lifetime | Maps to `command_workspace_id` | Yes, SQLite |
| `command_workspace_id` | Command Center Workspace Registry | No while ACTIVE | Deployment/workspace lifetime | Referenced by Command tasks and War project mapping | Yes, registry JSON |
| `war_task_id` | War Room, generated by War Room | No | Project task lifetime | References one or more `command_task_id` values | Yes, SQLite |
| `command_task_id` | Command Center Task Intake | No | Command task/audit lifetime | Referenced by Run Registry and War task mapping | Yes, JSONL/audit |
| `war_delivery_id` | War Room legacy delivery layer | No | Legacy delivery lifetime | During compatibility, references one `command_run_id` | Yes, SQLite |
| `command_run_id` | **Future Command Center owner/generator** | No | Execution attempt lifetime | Referenced by War delivery/timeline/evidence | **Not implemented as a separate field yet** |
| `war participant principal_id` | War Room membership/identity source | No within membership record | Participant membership lifetime | Maps to `agent_registry.agent_id` only when principal type is agent | Yes, SQLite |
| `agent_registry.agent_id` | Command Center Agent Registry | No while configured | Agent configuration lifetime | Referenced by Command task/Run Worker | Yes, registry JSON |
| `war session agent_id` | Legacy War Room/OpenClaw binding | No in a binding | Session binding lifetime | Validated against `worker_identity.worker_id`; never defines it | Yes, SQLite/OpenClaw store |
| `worker_identity.worker_id` | Command Center/Gateway request identity | No for one task/run | Task/run lifetime | References Agent Registry entry | Yes in result/audit evidence |
| `correlation_id` | Request initiator; War Room generates for War-originated request | No | End-to-end request lifetime | Must be echoed by Task Intake, Run Registry, Result/Evidence, and War timeline | War Room: yes; Command Center: **not implemented yet** |
| Command run `correlation_id` | Command Center persists the supplied immutable value | No | Run/audit lifetime | Links Run to War project/task/message/audit | **Not implemented yet** |

### 6.1 Cardinality

- One War project maps to one registered Command workspace for this phase. A workspace may serve multiple War projects only if policy explicitly permits it later.
- One War task may map to one or more Command tasks.
- Multi-agent War tasks compile into one Command task per Worker, with independent task IDs, authorizations, Runs, and evidence.
- One future `command_run_id` identifies one execution attempt. It must not be reused across Workers or retries.
- Until an explicit `command_run_id` exists, `task_id` must not be silently relabeled as a permanent run ID. A temporary compatibility alias requires an explicit versioned contract.

---

## 7. Execution Flow

Target flow:

```text
Human/Representative
  → War Room project/task/message/approval
  → War Room compatibility integration adapter
  → Command Center Task Intake
  → Workspace Registry + Path Guard
  → Agent Registry
  → Coordinator
  → Task Auth Broker
  → Fast Gateway
  → Worker
  → Run Registry + Result/Evidence
  → Run Query/Summary
  → War Room timeline/evidence/QA projection
```

### 7.1 War Room → Command Center request contract candidate

This is a design contract, not an implemented API:

```json
{
  "source": "war-room",
  "war_project_id": "immutable-war-project-id",
  "war_task_id": "immutable-war-task-id",
  "command_workspace_id": "plachem-agent-control",
  "original_instruction": "human-approved instruction",
  "requested_worker": "agent-registry-id",
  "requested_actions": ["read_only_review"],
  "correlation_id": "immutable-end-to-end-id"
}
```

Command Center must generate `command_task_id` and future `command_run_id`. War Room must not provide or choose them.

### 7.2 Command Center → War Room projection candidate

```json
{
  "war_project_id": "immutable-war-project-id",
  "war_task_id": "immutable-war-task-id",
  "command_task_id": "command-generated-task-id",
  "command_run_id": "command-generated-run-id",
  "correlation_id": "echoed-end-to-end-id",
  "status": "CREATED|DISPATCHING|RUNNING|PASS|FAIL|BLOCKED",
  "result_evidence_ref": "immutable-command-evidence-reference"
}
```

War Room stores/references this projection for timeline, evidence presentation, and QA. It cannot update the Command status.

---

## 8. Compatibility Strategy

### Stage 0 — Contract freeze (this phase)

- Preserve SQLite and all existing War Room code.
- Do not call OpenClaw, Hermes, Athena, or a Worker.
- Do not change schemas or create integration APIs.

### Stage 1 — Explicit compatibility adapter

- Add a narrowly scoped adapter in a later authorized phase.
- Adapter translates War project/task intent to Command Task Intake.
- Adapter is disabled by default until contract tests and positive Worker E2E pass.
- Existing `war_room_adapter.py` and `war_room_gateway_bridge.mjs` remain classified as Legacy/OpenClaw compatibility.

### Stage 2 — Command-authoritative projection

- War Room reads Run state from Command Run Query/Summary.
- Do not dual-write execution status as two authorities.
- Existing delivery/task execution columns may remain for read compatibility but must be marked legacy/projected.

### Stage 3 — Legacy execution freeze

- Stop creating authoritative `war_deliveries` and `war_task_calls` for integrated projects.
- Disable direct `chat.send`, polling, retry, and abort paths for integrated execution.
- Retain historical rows and UI compatibility readers.

### Stage 4 — Removal decision

- Remove Legacy adapters only after migration verification, historical-read compatibility, explicit rollback criteria, and separate approval.
- No deletion is authorized by this contract.

---

## 9. Deprecated State Plan

| State | Current use | Transition plan | Removal gate |
|---|---|---|---|
| `war_deliveries.status/run_id` | Current delivery execution truth | Read-only compatibility projection to Command Run | All integrated projects read Command Run; historical UI verified |
| `war_task_calls` | Call/turn execution counters | Replace display with Command/Gateway evidence | Counter parity and audit mapping verified |
| `war_tasks.running/completed` as execution meaning | Mixed project and execution status | Retain only collaboration meaning; expose Command status separately | UI and API consumers stop treating it as execution truth |
| `war_project_sessions` execution ownership | OpenClaw run/session binding | Legacy compatibility only | Command path no longer depends on OpenClaw session binding |
| `war_project_control` direct stop/abort | Calls Legacy adapter | Translate human intent to future Command control API | Command control contract implemented and verified |

Historical rows remain immutable/readable. No SQLite migration or deletion occurs in this phase.

---

## 10. OpenClaw Integration Boundary

### 10.1 Forbidden target path

```text
War Room → OpenClaw chat.send → Agent
```

War Room must never use `chat.send`, `agent.wait`, polling, or `chat.abort` to bypass Command Center for integrated execution.

### 10.2 Required target path

```text
War Room → Command Center Task Intake → Coordinator → Fast Gateway → Worker
```

### 10.3 Legacy preservation

- `war_room_adapter.py`, `war_room_gateway_bridge.mjs`, `war_room_worker.py`, and `war_room_runtime.py` are retained without rewrite.
- Existing disposable-session protections remain mandatory for any explicitly authorized Legacy/OpenClaw compatibility test.
- Business/work sessions must never be replaced, modified, rebound, or used as disposable test sessions.
- Existing tests that require the same persistent Gateway connection for send/abort remain historical Legacy safety contracts, not the target execution API.
- The OpenClaw server is currently offline; no live call was made for this contract.

---

## 11. Phase 2 #9 Implementation Candidates

These are candidates only; none are implemented here.

1. Extend Task Intake with required `project_id`, immutable external source references, and `correlation_id`.
2. Add explicit Command `run_id` distinct from `task_id`, including backward-compatible Run Registry/Run Query parsing.
3. Define a versioned in-process integration port before considering HTTP/REST.
4. Add an immutable mapping record for War project/task references to Command workspace/task/run IDs without making War Room execution-authoritative.
5. Compile one War multi-agent task into ordered independent Command tasks, one Worker/authorization/Run each.
6. Add read-only Command Run projection into War timeline/evidence/QA.
7. Define a future Command control-intent contract for stop/cancel; do not reuse current War adapter stop directly.
8. Add contract tests proving Broker/Gateway/Worker calls are zero on workspace/ID/auth mismatch.
9. Add compatibility feature flags and fail-closed routing so an integrated project cannot fall back silently to `chat.send`.
10. Complete positive Worker E2E when the Qwen endpoint is online before enabling any integration route.

---

## 12. Risks / Blockers

1. **No explicit Command run ID:** current Run Registry keys by `task_id`; `war_delivery_id → command_run_id` cannot be finalized yet.
2. **No Command correlation ID:** end-to-end correlation cannot be persisted in Run Registry/Result Evidence yet.
3. **Task Intake gap:** current intake does not carry `project_id` or external War references, while Dispatcher requires a registered project ID.
4. **Fan-out mismatch:** War Room can assign several agents to one task; Command tasks currently identify one Worker. Compilation rules are required.
5. **Status vocabulary collision:** War Room collaboration states and Command execution states overlap semantically but are not equivalent.
6. **Approval ambiguity:** War human approval and Task Auth Broker authorization are different records and must never be conflated.
7. **Control gap:** Command Center does not yet expose pause/resume/cancel/project-stop; current War direct stop/abort cannot be the target path.
8. **Evidence reference gap:** no immutable cross-system evidence-reference field exists yet.
9. **Legacy fallback risk:** existing adapter/runtime can still invoke OpenClaw; integration must fail closed rather than silently falling back.
10. **Source drift:** this contract is grounded in War Room commit `68467634412677858e3b42f86b5f4707d69b234c`; later changes require re-audit.
11. **Runtime blocker:** positive Achilles Worker E2E remains pending while `127.0.0.1:8080` is offline.
12. **OpenClaw blocker:** the server is offline, and live OpenClaw verification is intentionally not performed.

---

## 13. Explicit Non-Goals

This phase does **not**:

- implement integration code or a new adapter;
- add REST/HTTP endpoints or a server;
- modify War Room code, UI, tests, or SQLite schema;
- migrate SQLite to Supabase or create Supabase tables;
- modify Gateway, Broker, Policy, Test Harness, Hermes, or Athena;
- invoke OpenClaw, Hermes, Athena, or any Worker;
- add retry, polling, background processing, pause/resume/cancel, or control behavior;
- create a new War Room, redesign its UI, or delete Legacy code;
- create a Worktree or operate outside the official Command Center ROOT.

---

## 14. Acceptance Invariants

Integration work in a later phase must fail unless all are true:

1. Command Center generated the task and run identifiers.
2. Workspace Registry validated the canonical root and branch before authorization or dispatch.
3. Agent Registry resolved the Worker identity.
4. Coordinator owns ordering and creates no next Run after FAIL/BLOCKED.
5. Gateway is the only Worker invocation path.
6. Run Registry is the only execution lifecycle authority.
7. War Room stores only immutable Command references/projections for execution.
8. War Room human approval is not treated as execution authorization.
9. No integrated request can fall back to direct OpenClaw `chat.send`.
10. SQLite remains the War Room collaboration store until a separately approved migration.
