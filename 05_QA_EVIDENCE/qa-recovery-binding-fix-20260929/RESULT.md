# QA false-REWORK recovery binding fix

- Owner: ERPcoder
- Base: `04cb14dffbf2be3b22c0ee59aa18e1c879b21676`
- Scope: accept the same execution row's exact `core_run_id` or `openclaw_run_id` for Worker delivery binding; allow legacy NULL delivery `session_key` only with exact task/project/agent/revision and run-ID binding; reject non-NULL session mismatches.
- Source evidence read-only: `/home/plachem-sever/codex-workspaces/cha-secretary/plachem-ai-server-monitor/05_QA_EVIDENCE/flow-continuation-20260928/qa-recovery-state-1790649500/war-room.sqlite3`
- Source evidence shape: task `92986244-dfee-43cb-a075-90ab6de8bb96`, core `war-32754668-6922-49b2-aae9-4f0bc6c75a2c`, OpenClaw/delivery run `32754668-6922-49b2-aae9-4f0bc6c75a2c`, delivery `session_key=NULL`.

## Verification

- `python3 -m pytest -q tests/test_qa_false_rework_recovery.py`: PASS, 1 passed.
- `python3 -m pytest -q tests/test_qa_false_rework_recovery.py tests/test_result_only_revalidation_wiring.py tests/test_war_room_multi_fixes.py tests/test_war_room_auto_qa.py`: PASS, 19 passed, 4 existing FastAPI deprecation warnings.
- `python3 -m py_compile war_room_actions.py war_room_stage_recovery.py`: PASS.
- `git diff --check`: PASS.

Regression covers candidate discovery, actual-shaped prefixed core/OpenClaw IDs, NULL legacy delivery session, foreign run rejection, non-NULL session mismatch rejection, cancellation/approval/reason/evidence checks, concurrency, and idempotent replay.

No operating repo/DB/service/config modification, deployment, push, or existing isolated evidence-source modification was performed.
