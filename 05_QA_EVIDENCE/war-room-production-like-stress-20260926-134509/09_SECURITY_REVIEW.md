STRESS_SECURITY_PASS

# Independent security/logic review

## Verdict

**REWORK** — no P0 found; one P1 logic/semantic blocker remains.

## Scope and verification

- Reviewed the isolated `repo_snapshot` only; no source or production state was modified.
- Executed independently:
  - `python3 -m pytest -q tests/test_readiness_api.py tests/test_process_board_api.py tests/test_process_board_ui.py` → **9 passed** (4 framework deprecation warnings).
  - `python3 -m compileall -q war_room_actions.py` → passed.
  - `git diff --check` → passed.
- The readiness endpoint uses `_connect_ro()` with SQLite `mode=ro` and `PRAGMA query_only = ON` at `war_room_actions.py:1468-1471` and performs no commit/write operation.
- Authentication and project membership are enforced both by the readiness handler (`war_room_actions.py:1469-1470`) and the HTTP middleware (`app.py:127-145`). Actor-only requests do not authenticate without a valid trusted proxy/session/token path.

## Findings

### P1 — readiness can falsely claim representative-completion readiness

`project_readiness()` marks the project ready when there is at least one non-SUPERSEDED task and no task mapped to a blocking state (`war_room_actions.py:1473-1500`). That projection treats `PASS` and `DONE` as sufficient. The UI consequently renders **“대표 완료 준비됨”** and says all active tasks are PASS/DONE (`static/war-room-ui.js:107-114`).

The actual representative-completion mutation has stricter requirements: the task must still be in `qa`, and approval requires task/revision/document/QA-cycle-bound evidence plus a signed QA PASS (`war_room_actions.py:2022-2030`); when required by the packet it also requires verified session-integrity evidence (`war_room_actions.py:2031-2040`). Therefore the read-only banner can report readiness even though the corresponding representative approval would be rejected, especially for a task projected as `DONE` without the current evidence contract or for a `PASS` projection lacking evidence.

Impact: no direct authorization bypass—the mutation endpoint independently enforces representative identity and evidence—but the control-plane status is misleading and can cause premature completion decisions. Align readiness with the same approval predicate, or rename/downgrade the field to a non-authoritative lifecycle summary and expose the missing evidence/conditions explicitly. Add a regression test covering PASS/DONE without bound evidence and `session_integrity_required=true`.

### No P0/P1 security bypasses observed in the remaining checks

- **Authentication/read boundary:** readiness requires authenticated project membership with read capability; project IDs are parameterized SQL values. The global War Room GET middleware also applies project membership checks to Process Board routes.
- **Information leakage:** readiness returns counts and task IDs only. Process Board returns task projection data only after the same HTTP read gate; response rendering uses escaping for task/user-controlled text. Session keys/IDs are omitted from the delivery-list response, and the reviewed readiness response does not include them.
- **SUPERSEDED semantics:** superseding is represented by an append-only audit event and takes precedence in the board mapping (`war_room_actions.py:180-190`, `220-253`); readiness excludes those IDs from blocking and reports them separately (`war_room_actions.py:1479-1487`). Replacement validation requires same-project membership and latest QA PASS (`war_room_actions.py:1547-1563`).
- **DB mutation risk:** the readiness path is read-only at the SQLite connection level and the byte-immutability test passed. The Process Board handler uses the normal connection but performs only SELECTs; it is not the readiness mutation path.
- **Path/UI injection:** no readiness path constructs filesystem paths from request data. UI task IDs, labels, states, and readiness blocking IDs are escaped before HTML insertion; the readiness success text interpolates only a numeric count and array length.

## Completion-condition assessment

- `09_SECURITY_REVIEW.md` exists: yes.
- `STRESS_SECURITY_PASS` present: yes.
- Independent verification executed: yes; results recorded above.
- Production/source changes by this task: none; only this report artifact was written.
