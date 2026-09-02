# War Room ↔ Command Center API Contract

**Phase:** 2 #18
**Status:** Contract only — HTTP implementation is not included
**Command Center:** `E:\PLACHEM-Agent-Control\repo`, branch `phase2-worker-identity`
**Command Center baseline:** `9f2915f878ce7045d57835d36b3f933c0df03d57`
**Existing War Room:** `egokiller01-lab/plachem-ai-server-monitor`, branch `main`
**Existing War Room reviewed HEAD:** `68467634412677858e3b42f86b5f4707d69b234c`

This document freezes the boundary between the existing War Room collaboration system and the Command Center orchestration interface. It does not change the War Room repository, its SQLite database, or the Command Center implementation.

---

## 1. Architecture

The target path for a new Command Center-managed task is:

```text
Authenticated War Room principal
        ↓
Existing War Room backend / integration adapter
        ↓
Command Center API contract
        ↓
WarRoomOrchestrator
        ↓
War Room Task Adapter → Multi-Agent Compiler
        ↓
Dependency Readiness → Candidate Selector
        ↓
Explicit Dispatch Boundary
        ↓
Existing Dispatcher → Fast Gateway
        ↓
Selected Agent Runtime Profile → Worker
```

The War Room remains the human/project collaboration layer. Command Center is the execution authority.

```text
War Room owns:       project, participants, messages, human approval, QA presentation
Command Center owns: task routing, authorization handoff, dispatch, Run state, execution evidence
```

The War Room must not invoke a Worker directly for a new `command_center` task.

### 1.1 Existing War Room implementation reviewed

The existing repository was reviewed read-only at the commit stated above:

- `war_room.py`: FastAPI read model, project/participant/message/session reads, principal resolution and redaction.
- `war_room_actions.py`: controlled SQLite mutations, task/approval/QA/evidence/audit records, Idempotency-Key handling, and legacy delivery records.
- `war_room_worker.py`: explicit legacy delivery processing, retries, recovery, and stop timers.
- `war_room_adapter.py`: test adapter plus opt-in OpenClaw session adapter and gateway bridge.
- `README.md`: existing `/api/war-room` read endpoints, authenticated controlled writes, server-side principal token mapping, proxy identity, and Idempotency-Key requirements.

The existing War Room direct execution path remains available only for legacy compatibility during migration. It is not part of this Command Center API contract.

---

## 2. Endpoint Matrix

| Operation | Method and path | Orchestrator method | Side effect | Authenticated principal | Idempotency |
|---|---|---|---|---|---|
| Submit workflow | `POST /api/command-center/war-room/tasks` | `submit(payload)` | Creates an in-process Command workflow compilation; does not execute a Worker | Required | Required |
| Workflow status | `GET /api/command-center/war-room/projects/{project_id}/tasks/{war_task_id}` | `status(project_id, war_task_id)` | None | Required | Not applicable |
| Candidates | `GET /api/command-center/war-room/projects/{project_id}/tasks/{war_task_id}/candidates` | `candidates(project_id, war_task_id)` | None | Required | Not applicable |
| Dispatch one child | `POST /api/command-center/tasks/{task_id}/dispatch` | `dispatch(task_id)` | Dispatches exactly one selected child through the Boundary | Required | Required |
| Next READY child | `GET /api/command-center/war-room/projects/{project_id}/tasks/{war_task_id}/next-ready` | `next_ready(project_id, war_task_id)` | None | Required | Not applicable |
| Summary | `GET /api/command-center/war-room/projects/{project_id}/tasks/{war_task_id}/summary` | `summary(project_id, war_task_id)` | None | Required | Not applicable |

HTTP is a future transport. The contract does not authorize implementing these routes in Phase #18.

---

## 3. Submit Request Contract

### 3.1 JSON request

```json
{
  "war_project_id": "plachem-agent-war-room",
  "war_task_id": "war-task-001",
  "scope": "Inspect the registered project and prepare the requested work",
  "requested_agents": ["Achilles", "Athena"],
  "workspace_id": "plachem-agent-control",
  "required_capabilities": ["coding", "review"],
  "preferred_worker": "Achilles",
  "selection_mode": "strict",
  "workflow": [
    {
      "agent_id": "Achilles",
      "role": "implementation",
      "depends_on": []
    },
    {
      "agent_id": "Athena",
      "role": "review",
      "depends_on": ["Achilles"]
    }
  ],
  "correlation_id": "war-correlation-001",
  "requested_actions": ["read_only_review"],
  "approval": {
    "status": "approved",
    "approval_id": "war-approval-001"
  },
  "metadata": {
    "source_message_id": "war-message-001"
  }
}
```

### 3.2 Accepted fields

| Field | Required | Meaning |
|---|---:|---|
| `war_project_id` | Yes | Existing War Room project identity |
| `war_task_id` | Yes | Existing War Room task identity |
| `scope` | Yes | Task scope/instruction, string or structured object |
| `requested_agents` | Yes, or `assignee_agent_id` | Agent identity hints used by the existing compiler |
| `assignee_agent_id` | Alternative | Single requested Agent identity |
| `workspace_id` / `command_workspace_id` | Yes | Registered Workspace ID; never a raw path |
| `required_capabilities` | Optional | Capability requirements for later routing |
| `preferred_worker` | Optional | Preferred, not forced, Worker in `fallback` mode |
| `selection_mode` | Optional | `strict` by default; `fallback` only when explicitly supplied |
| `workflow` | Optional | Existing compiler workflow roles and dependencies |
| `correlation_id` | Optional | Caller-supplied immutable end-to-end identity; generated if absent |
| `requested_actions` | Optional | Existing Broker/Gateway action vocabulary |
| `approval` | Optional | Human approval metadata only |
| `metadata` | Optional | Presentation/correlation metadata; must not contain secrets |

### 3.3 Forbidden request fields

The request must reject or ignore neither silently nor partially; the API layer must return `INVALID_REQUEST` for these fields:

```text
model
provider
endpoint
api_key
secret
credential
context_size
project_root
workspace_root
workspace_path
path
```

The request carries Agent identity only. Runtime Profile details are resolved by the selected Agent execution path and are not part of this API contract.

### 3.4 Submit response

```json
{
  "war_project_id": "plachem-agent-war-room",
  "war_task_id": "war-task-001",
  "correlation_id": "war-correlation-001",
  "external_reference": {
    "source": "war_room",
    "project_id": "plachem-agent-war-room",
    "external_task_id": "war-task-001"
  },
  "selection_mode": "strict",
  "command_tasks": [
    {
      "task_id": "task-generated-by-command-center",
      "requested_agent": "Achilles",
      "selected_worker": "Achilles",
      "workflow_role": "implementation",
      "depends_on_task_ids": [],
      "readiness": "READY",
      "run_id": null,
      "run_status": null
    }
  ],
  "workflow_graph": [
    {
      "task_id": "task-generated-by-command-center",
      "role": "implementation",
      "depends_on_task_ids": [],
      "dependency_mode": "all_success"
    }
  ]
}
```

Submit never dispatches, creates a Run, calls a Worker, or creates Task Auth Broker authorization merely because War Room human approval metadata is present.

---

## 4. Status Contract

### Request

```text
GET /api/command-center/war-room/projects/{war_project_id}/tasks/{war_task_id}
```

### Response

```json
{
  "war_project_id": "plachem-agent-war-room",
  "war_task_id": "war-task-001",
  "correlation_id": "war-correlation-001",
  "tasks": [
    {
      "task_id": "command-task-001",
      "requested_agent": "Achilles",
      "selected_worker": "Achilles",
      "workflow_role": "implementation",
      "depends_on_task_ids": [],
      "readiness": "READY",
      "run_id": null,
      "run_status": null,
      "result_available": false
    }
  ]
}
```

`status()` is read-only. It derives readiness using the existing `DependencyReadinessEvaluator` and Run fields using the existing `RunQuery`. It must not create a Run, mutate a task, consume authorization, append an audit event, or dispatch a child.

The authoritative execution status vocabulary is:

```text
CREATED → DISPATCHING → RUNNING → PASS | FAIL | BLOCKED
```

War Room collaboration states such as `draft`, `awaiting_approval`, `qa`, `completed`, and `stopped` are not equivalent to these execution states.

---

## 5. Candidates Contract

```text
GET /api/command-center/war-room/projects/{war_project_id}/tasks/{war_task_id}/candidates
```

Response:

```json
{
  "candidates": [
    {
      "task_id": "command-task-001",
      "requested_worker": "Achilles",
      "workflow_role": "implementation",
      "readiness": "READY",
      "reason": "dependencies satisfied"
    }
  ],
  "excluded": [
    {
      "task_id": "command-task-002",
      "readiness": "WAITING",
      "reason": "dependency command-task-001 is still pending"
    }
  ]
}
```

The API must call the existing `DispatchCandidateSelector`. It must not implement a second readiness algorithm, auto-dispatch candidates, or fan out across all candidates.

---

## 6. Dispatch Contract

### Request

```text
POST /api/command-center/tasks/{task_id}/dispatch
```

Optional body:

```json
{
  "idempotency_key": "war-dispatch-001"
}
```

The HTTP header `Idempotency-Key` is preferred; a body field is not a substitute when the transport requires the header.

### Required internal path

```text
WarRoomOrchestrator.dispatch(task_id)
        ↓
ExplicitDispatchBoundary.dispatch_selected(task_id, tasks)
        ↓
Existing Dispatcher
        ↓
Fast Gateway
```

The Orchestrator must not call `task_dispatch.dispatch`, Fast Gateway, or a Worker directly from its public dispatch method. The Boundary performs candidate/readiness revalidation and forwards one task only.

### Response

```json
{
  "war_project_id": "plachem-agent-war-room",
  "war_task_id": "war-task-001",
  "correlation_id": "war-correlation-001",
  "task_id": "command-task-001",
  "requested_agent": "Achilles",
  "selected_worker": "Achilles",
  "run_id": "run-generated-by-command-center",
  "run_status": "RUNNING",
  "gateway_status": "RUNNING"
}
```

A successful HTTP acceptance means only that the Command Center accepted the explicit dispatch request. Worker PASS, Gateway PASS, and final workflow completion remain separate results.

---

## 7. Next READY and Summary Contracts

### 7.1 Next READY

```text
GET /api/command-center/war-room/projects/{war_project_id}/tasks/{war_task_id}/next-ready
```

Response:

```json
{
  "items": [
    {
      "task_id": "command-review-001",
      "requested_agent": "Athena",
      "workflow_role": "review",
      "readiness": "READY",
      "depends_on_task_ids": ["command-implementation-001"]
    }
  ]
}
```

This is a query only. It must not dispatch the returned task.

### 7.2 Summary

```text
GET /api/command-center/war-room/projects/{war_project_id}/tasks/{war_task_id}/summary
```

Response:

```json
{
  "war_project_id": "war-project-001",
  "war_task_id": "war-task-001",
  "correlation_id": "war-correlation-001",
  "overall": "READY",
  "tasks": []
}
```

`overall` is derived, not stored:

| Overall | Rule |
|---|---|
| `COMPLETED` | Every required non-observer child has terminal `PASS` |
| `IN_PROGRESS` | At least one required child has an active Run |
| `READY` | No active Run and at least one executable child is `READY` |
| `PENDING` | No active Run, no READY child, and a required child is waiting on a dependency |
| `BLOCKED` | A required child cannot proceed because of a prerequisite/dependency block |
| `FAILED` | A required child has terminal `FAIL` or `BLOCKED` and completion cannot be reached |

`observer` children are visible but excluded from the completion gate.

---

## 8. Error Contract and HTTP Mapping

The HTTP layer must map internal exceptions to stable public errors. It must not expose tracebacks, file paths, credentials, provider details, or raw internal exception text.

| Public code | Recommended HTTP status | Meaning |
|---|---:|---|
| `INVALID_REQUEST` | 400 | Malformed JSON, forbidden field, invalid selection mode, invalid workflow shape |
| `UNKNOWN_AGENT` | 422 | Requested Agent is not in the active Agent Registry |
| `UNKNOWN_WORKSPACE` | 422 | Workspace ID is not registered or active |
| `DUPLICATE_EXTERNAL_REFERENCE` | 409 | Same War project/task already has a Command compilation |
| `REVISION_REQUIRED` | 409 | Agent set, dependency graph, role, or assignment structure changed |
| `TASK_NOT_FOUND` | 404 | Command child or workflow reference does not exist |
| `TASK_NOT_READY` | 409 | Explicit task is waiting or blocked by dependency |
| `TASK_ALREADY_ACTIVE` | 409 | Explicit task already has an active Run |
| `TASK_ALREADY_TERMINAL` | 409 | Explicit task already has a terminal Run |
| `NO_AVAILABLE_WORKER` | 409 | No enabled, capability-compatible, available Worker exists |
| `AUTHORIZATION_REQUIRED` | 403 | Task Auth Broker authorization is absent, expired, revoked, mismatched, or unavailable |
| `DISPATCH_REJECTED` | 409 | Boundary, Dispatcher, policy, or Gateway rejected the explicit dispatch |
| `INTERNAL_ERROR` | 500 | Unexpected server error; response contains only a correlation/reference ID |

Common error body:

```json
{
  "error": {
    "code": "TASK_NOT_READY",
    "message": "The selected Command task is not ready for dispatch.",
    "correlation_id": "war-correlation-001"
  }
}
```

The internal reason may be logged in the owning system's audit channel but is not automatically returned to the War Room client.

---

## 9. Authentication Boundary

The existing War Room remains the authentication and principal source. The future integration adapter passes the verified principal as request actor context; it does not create a second authentication system.

Existing War Room authentication mechanisms observed in `war_room.py` and `war_room_actions.py` include:

- server-side `PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS` mapping with `X-War-Room-Token`;
- optional actor header checked against the authenticated principal;
- trusted reverse-proxy identity using `X-Authenticated-Principal` or `X-Forwarded-User` plus the configured proxy secret;
- HttpOnly signed `war_room_session` cookie for browser access.

The future adapter contract is:

```text
War Room authenticated principal
        → request actor / principal context
        → Command Center audit context
```

The Command Center must not trust a caller-supplied actor ID without the War Room authentication boundary validating it. Principal propagation is identity context, not Task Auth Broker authorization.

---

## 10. Authorization Boundary

These records remain separate:

```text
War Room Human Approval
        !=
Command Center Task Auth Broker Authorization
```

Rules:

1. `submit` may carry human approval metadata for workflow presentation.
2. Human approval must not create, consume, renew, or substitute for Broker authorization.
3. `dispatch` proceeds only through the existing Dispatcher/Broker contract.
4. Broker authorization is bound to the actual selected Worker, Task, actions, expiry, revocation state, and integrity checks.
5. If fallback changes the Worker, the authorization target must be the selected Worker, not the preferred Worker.
6. Broker rejection is returned as `AUTHORIZATION_REQUIRED` or `DISPATCH_REJECTED` according to the failure stage.

No API endpoint in this contract creates a new authorization store.

---

## 11. Idempotency

The existing War Room controlled writes use `Idempotency-Key` and persist request hash plus response under an actor/scope/key tuple. Command Center integration must preserve that property without trusting the key as an authorization credential.

### 11.1 Submit

```text
scope = POST:/api/command-center/war-room/tasks
identity = authenticated War Room principal
key = Idempotency-Key
request_hash = canonical JSON hash of the validated request
```

- Same principal + same scope + same key + same request hash: return the original response; create zero new child tasks.
- Same principal + same scope + same key + different request hash: return `INVALID_REQUEST` or `409` idempotency conflict; do not compile.
- A key from another principal is not reusable.
- Duplicate external reference and Idempotency-Key are independent protections; both must remain active.

### 11.2 Dispatch

```text
scope = POST:/api/command-center/tasks/{task_id}/dispatch
identity = authenticated War Room principal
key = Idempotency-Key
request_hash = canonical JSON hash of task ID and dispatch options
```

- Same request returns the original dispatch acceptance/result.
- A different request under the same key is rejected.
- Idempotency does not permit a second Run, retry, fan-out, or authorization reuse after the original execution contract is consumed.

Phase #18 adds no persistence. Phase #19 must choose the existing War Room idempotency record or an already-approved Command Center integration port before HTTP write routes are enabled.

---

## 12. Existing War Room Mapping

| Existing War Room concept | Command Center contract mapping | Authority after integration |
|---|---|---|
| `war_tasks` | One War task submitted to a Command workflow; may produce multiple child tasks | War Room collaboration record; Command Center owns execution references |
| `war_task_agents` | `requested_agents`, workflow/assignment hints | War Room intent only; Registry/Orchestrator select execution Worker |
| `war_deliveries` | `command_task_id` / Run reference during migration | Legacy compatibility/reference only for integrated tasks |
| `war_task_calls` | Run/Gateway execution evidence and attempt data | Command Center execution evidence; War counter is non-authoritative |
| `war_evidence` | Reference to immutable Command Result/Evidence | War Room presentation; Command Center owns execution truth |
| `war_project_control` | Future control-intent contract | War Room owns human intent; current Command Center has no stop/cancel API |
| `war_project_sessions` | Legacy session/channel compatibility only | Never Worker identity or Run ownership |
| `war_approvals` | Approval metadata in submit/workflow context | Human approval only; never Broker authorization |
| `war_qa_verdicts` | Human QA over referenced Command evidence | War Room QA authority; does not override Command Run status |
| `war_audit_events` | Presentation/audit reference to Command events | War Room collaboration audit; Command audit remains authoritative for execution |

The Command Center generates child `task_id` values and future `run_id` values. War Room does not supply or select them.

---

## 13. Legacy Compatibility and Execution Mode

### 13.1 Execution mode

Future War Room requests may carry:

```json
{
  "execution_mode": "command_center"
}
```

Allowed values:

```text
command_center
legacy
```

Contract defaults:

```text
new integration request → command_center
existing historical task → legacy (only when explicitly classified)
```

This phase does not modify the War Room schema. The future adapter must determine the mode explicitly and persist or derive it using an approved existing integration mechanism.

### 13.2 No silent fallback

```text
execution_mode=command_center
    → Command Center API only

execution_mode=legacy
    → existing War Room legacy path only
```

A Command Center API error must not silently call `war_room_worker`, `chat.send`, `agent.wait`, polling, or `chat.abort`. A legacy failure must not silently be retried through Command Center.

### 13.3 Legacy path retained

The following remain unchanged and are not deleted in this phase:

```text
war_room_worker.py
war_room_runtime.py
war_room_adapter.py
war_room_gateway_bridge.mjs
```

Their existing disposable-session, run-ownership, recovery, timeout, and explicit stop safeguards remain applicable only to explicitly authorized legacy tests or legacy tasks.

---

## 14. Runtime Profile and Agent Contract

The API exposes Agent identity only:

```text
Achilles
Athena
ERPcoder
ERPqa
```

It must not accept or return the following as routing inputs:

```text
model
provider
endpoint
context size
local/remote backend
API key
```

The selected Agent's current Runtime Profile is resolved downstream by the existing Agent Registry/Fast Gateway execution path. Changing a profile must not require changing this API contract or the Orchestrator API shape.

---

## 15. Security Invariants

Future HTTP implementation must preserve all of the following:

1. Official registered Workspace ID is required; raw filesystem paths are rejected.
2. Workspace canonical path, active status, and branch are validated before authorization or dispatch.
3. Unknown Agent IDs fail closed.
4. `strict` is the default selection mode.
5. Fallback is available only when explicitly requested and only for capability-compatible available Workers.
6. `selected_worker` is the Worker used for Broker authorization and execution.
7. Candidate queries are read-only.
8. `next_ready` never auto-dispatches.
9. Dispatch passes through `ExplicitDispatchBoundary` and forwards one child only.
10. War Room approval never creates Broker authorization.
11. Run Registry is the execution lifecycle authority.
12. Result/Evidence is referenced, not recreated as an independent execution truth.
13. Integrated requests cannot silently fall back to OpenClaw direct execution.
14. Idempotency key reuse with a different request hash is rejected.
15. API errors do not disclose secrets, raw paths, provider credentials, or stack traces.
16. No HTTP handler creates schema, mutates the War Room SQLite database, or changes the Command Center Runtime merely by reading status.

---

## 16. Phase #19 Implementation Scope

Phase #19 may implement the smallest bounded integration port, subject to a separate task approval:

1. Select one HTTP framework already present in the Command Center environment, or use a thin standard-library/in-process adapter; do not introduce a large framework without need.
2. Implement the six routes in the Endpoint Matrix as transport wrappers around `WarRoomOrchestrator`.
3. Validate the request schema and reject forbidden Runtime Profile fields and raw paths.
4. Resolve the authenticated War Room principal from an existing trusted integration boundary; do not create a second login system.
5. Map internal exceptions to the stable Error Contract.
6. Preserve Idempotency-Key semantics using an approved existing persistence/port; do not add an unapproved database.
7. Add contract tests for JSON shape, error mapping, principal propagation, idempotency conflict, strict/fallback mode, and zero direct Worker/Gateway bypass.
8. Add one explicit feature flag or route-level mode gate that defaults integrated traffic to fail-closed until positive verification is complete.
9. Verify that Command Center dispatch remains `Orchestrator → Boundary → Dispatcher → Fast Gateway`.
10. Keep the existing War Room repository and SQLite schema unchanged until a separately approved migration phase.

Phase #19 must not enable production integration merely because HTTP responses serialize correctly. A positive Worker E2E and independent authorization/path verification remain separate gates.

---

## 17. Explicit Non-Goals

Phase #18 does not:

- implement HTTP endpoints, FastAPI routes, WebSocket, or UI;
- modify the existing War Room repository, code, tests, or SQLite schema;
- clone the War Room repository or create a Worktree;
- modify Command Center Gateway, Broker, Policy, Runtime, or Worker configuration;
- call OpenClaw, Hermes, Fast Gateway, or any Worker;
- create a new persistence engine or Supabase table;
- add stop/cancel/pause/resume control;
- add retries, polling, scheduling, background processing, or fan-out;
- delete or rewrite the legacy War Room adapter/worker path;
- accept model/provider/endpoint/API-key fields in the API contract;
- treat War Room approval, delivery status, or message response as Command execution success.

---

## 18. Contract Acceptance Invariants

A future implementation is not accepted unless all are true:

1. Submit creates no Worker execution.
2. Every Command child has an independent Command task ID.
3. War project/task/correlation identity is preserved.
4. Status, candidates, next-ready, and summary are read-only.
5. Dispatch forwards exactly one child through the Explicit Dispatch Boundary.
6. Strict is the default; fallback is explicit.
7. No capability-incompatible or unavailable fallback is selected.
8. Human approval and Task Auth Broker authorization remain separate.
9. Runtime Profile details are absent from the API request contract.
10. Duplicate submit does not create new children.
11. Revision changes do not overwrite a previous compilation.
12. Run Registry/Query remains the execution status authority.
13. Legacy and Command Center modes cannot silently cross paths.
14. Idempotency conflicts fail closed.
15. War Room code, SQLite, Runtime, Gateway, Broker, Policy, and Worker state remain unchanged in this contract-only phase.
