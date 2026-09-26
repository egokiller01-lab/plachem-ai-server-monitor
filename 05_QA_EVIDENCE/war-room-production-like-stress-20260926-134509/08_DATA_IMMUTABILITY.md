STRESS_DATA_PASS

# 08 — Independent Data-Contract & Immutability Verification

- Worktree: `/home/plachem-sever/.openclaw/workspace/00_CONTROL/WAR_ROOM_STRESS_20260926-134509/repo_snapshot`
- Revision (grounding packet): `stress-readiness-v1`
- Verifier: ProcessData (independent of the implementation agent)
- Date: 2026-09-26 (Asia/Bangkok)
- Scope: read-only verification of the readiness projection as a data contract. No source code modification, no production repository/DB access, no service restart, no merge/push/deploy. All fixture databases were created in fresh `/tmp` directories via `PLACHEM_WAR_ROOM_DB`.

## 1. Implementation inspected (static review)

- `repo_snapshot/war_room_actions.py`
  - `GET /api/war-room/projects/{project_id}/readiness` (line ~1460) opens the DB via `_connect_ro()`, calls `_project_or_404`, requires read authorization via `_actor(..., "read", ...)`, then projects existing lifecycle data through `_process_board_projection`. No write, commit, provision, or mutation call on the readiness path.
  - `_connect_ro()` uses SQLite URI `mode=ro` plus `PRAGMA query_only=ON` (and `busy_timeout=2000`). Verified independently: an `INSERT` through `_connect_ro()` raises `sqlite3.OperationalError`.
  - `state_counts` is seeded with all nine `PROCESS_BOARD_STATES` (`WAITING, READY, RUNNING, PASS, FAIL, REWORK, BLOCKED, SUPERSEDED, DONE`) so every state key is always present, including zero counts.
  - Blocking rule: every non-`SUPERSEDED` task is counted as considered; a task is blocking when its mapped state is not in `{PASS, DONE}` (explicit `blocking_states` set plus a fail-closed `not in {PASS, DONE}` fallback — unknown states block). `SUPERSEDED` tasks are excluded from blocking and listed in `nonblocking_superseded_ids`.
  - `blocking_task_ids` and `nonblocking_superseded_ids` are both `.sort()`-ed → deterministic ascending order.
  - `ready_for_representative_completion` requires `considered_task_count > 0 AND not blocking_task_ids` → empty project is fail-closed (`False`).
  - State mapping `_process_board_state` precedence: superseded > delivery `system_error` (BLOCKED) > completed (DONE) > stopped/stop_unconfirmed (BLOCKED) > rework_required (REWORK) > latest QA verdict FAIL/REWORK/PASS > draft/awaiting_approval/qa (WAITING) > approved (READY) > running (RUNNING) > default WAITING.
- `repo_snapshot/tests/test_readiness_api.py` and `tests/test_process_board_api.py` reviewed; fixtures are temp-dir SQLite DBs and one test already byte-compares the DB before/after GET.

## 2. Independent dynamic verification (own fixture, own expectations)

Independent harness: `/tmp/warroom_readiness_verify/verify_readiness.py` (outside the snapshot; snapshot not modified). It builds its own 11-task fixture covering DONE, PASS, FAIL(verdict-beats-lifecycle), REWORK(lifecycle-beats-PASS), RUNNING, WAITING(draft), WAITING(awaiting_approval), READY, BLOCKED(stopped-beats-PASS), BLOCKED(delivery system_error), SUPERSEDED, and computes expected `state_counts`, `blocking_task_ids`, `nonblocking_superseded_ids`, and `considered_task_count` from the documented mapping rules **before** calling the API.

Observed result (raw run output, tail):

```text
PASS :: auth_no_header_401
PASS :: readiness_http_200
PASS :: determinism_three_gets_identical
PASS :: db_bytes_unchanged_after_gets
PASS :: row_counts_unchanged_after_gets
PASS :: state_counts_match_independent_expectation
PASS :: blocking_task_ids_match
PASS :: blocking_ids_sorted_ascending
PASS :: superseded_excluded_nonblocking
PASS :: considered_task_count
PASS :: not_ready_when_blocking
PASS :: mode_readonly
PASS :: all_done_ready_true
PASS :: all_done_blocking_empty
PASS :: all_done_superseded_still_nonblocking
PASS :: all_done_get_byte_immutable
PASS :: empty_project_not_ready
PASS :: connect_ro_rejects_write
RESULT: VERIFY_PASS
```

Key properties proven:

1. **state_counts correctness** — matches the independently computed expectation for all nine states, including precedence conflicts (QA FAIL on a draft → FAIL; QA PASS on a stopped task → BLOCKED; QA PASS on rework_required → REWORK; superseded beats everything).
2. **blocking_task_ids contents and ordering** — exact match to the independently derived set; ascending sort verified; `SUPERSEDED` excluded and surfaced in `nonblocking_superseded_ids`.
3. **Determinism** — three consecutive GETs return byte-identical JSON payloads.
4. **GET immutability** — SHA-256 of the entire SQLite file is unchanged across all readiness GETs, and per-table `COUNT(*)` over every table is unchanged (checked with a separate read-only connection). Also re-verified after the all-completed transition.
5. **Fail-closed** — missing auth → 401; unknown project → 403 (authorization path); empty project → `ready_for_representative_completion=false`, `considered_task_count=0`; any non-PASS/DONE, non-SUPERSEDED task blocks.
6. **Read-only enforcement** — write attempt via `_connect_ro()` rejected by SQLite (`query_only`).
7. **Append-only audit intact** — the fixture DB's `war_audit_events` DELETE trigger fired during harness setup adjustment, confirming the audit table's append-only guard is active in this schema; the readiness path never touches it.

## 3. Repository test re-run (fresh)

```text
$ python3 -m pytest -q tests/test_readiness_api.py tests/test_process_board_api.py
6 passed, 4 warnings in 1.73s
```

(4 warnings are pre-existing FastAPI `on_event` deprecation notices, unrelated to readiness logic.) The implementation report claims 9 passed including `tests/test_process_board_ui.py`; this verifier re-ran the two API test files relevant to the data contract (6 tests) plus the full independent harness above.

## 4. Boundary confirmation

- No file inside `repo_snapshot` was modified by this verification (harness lives in `/tmp/warroom_readiness_verify/`).
- No production repository, production database, service, or existing work session was touched; all DB access was against temp fixture files.
- No merge, push, deploy, or restart performed. **No representative completion is claimed by this verifier — only main can approve that.**

## 5. Residual risks (non-blocking for the data contract)

- Immutability was proven at file-byte and row-count level for single-connection fixture DBs; WAL sidecar behavior under concurrent production writers was not exercised (isolated fixture environment has no concurrent writer). The `mode=ro` + `query_only` connector makes writes structurally impossible from the readiness path regardless.
- UI-level readiness rendering remains structurally contract-tested only (no browser-driven visual check), same limitation the implementation report disclosed.

## Verdict

**PASS** — readiness calculation matches the independently derived contract; `state_counts` and `blocking_task_ids` contents/ordering are correct and deterministic; GET is provably byte- and row-count-immutability-preserving; auth and empty-state behavior are fail-closed.
