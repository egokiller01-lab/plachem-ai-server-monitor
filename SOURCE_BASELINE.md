# WAR-ROOM-SOURCE-SYNC-01 source baseline

Status: **SOURCE SNAPSHOT ONLY — PRODUCT QA NOT ASSERTED**

## Collection identity

- Collected at: `2026-09-07T21:46:11+07:00` (`Asia/Bangkok`)
- Live service source: `/home/plachem-sever/codex-workspaces/cha-secretary/plachem-ai-server-monitor`
- Live service at collection: `active/running`, PID `1724812`
- Original branch: `main`
- Original HEAD: `714c34dc25ac08f08b0be529f4bd5a8032cb6a69`
- Original tracking state: `main...origin/main [ahead 40]`
- GitHub repository: `https://github.com/egokiller01-lab/plachem-ai-server-monitor`
- GitHub visibility observed before push: `PUBLIC` (unchanged by this task)

The review branch is a snapshot of the live working tree, not merely the original
HEAD commit. Existing uncommitted source and test changes were copied into a
separate clone and committed there. The live working tree was not committed,
reset, stashed, cleaned, or otherwise modified.

## Included scope

- War Room and Fast Gateway Python source.
- Browser UI HTML and JavaScript.
- Unit, integration, runtime, and browser-contract tests present in the live tree.
- `requirements.txt` and the example systemd unit.
- Repository README and `docs/openclaw-project/` design/control documents.
- Current untracked source/test files `war_room_agents.py` and
  `tests/test_war_room_ui_integrated_fix.py`.

Exact included paths and their live-source SHA256 values are recorded in
`SOURCE_SNAPSHOT_FILES.sha256`.

## Excluded scope

- `.git/` repository internals.
- Raw `.env` files and credentials, tokens, cookies, passwords, or session data.
- `runtime/`, SQLite/DB files, JSONL run history, and operating bindings.
- Customer or operating business data.
- Generated caches and local dependency cache (`__pycache__`, `.pytest_cache`,
  `.deps`).
- `05_QA_EVIDENCE/`, including screenshots, execution evidence, and TEST_ONLY
  runtime dumps. Historical tracked evidence was intentionally removed from this
  review snapshot's tree; it remains preserved in the repository's prior history.

The exclusion policy is documented in `SOURCE_SNAPSHOT_EXCLUSIONS.md`.

## Live working-tree changes represented

Modified live files at collection:

- `app.py`
- `plachem_fast_gateway/core_engine.py`
- `plachem_fast_gateway/openclaw_adapter.py`
- `plachem_fast_gateway/tests/test_core_engine.py`
- `plachem_fast_gateway/tests/test_result_format_recovery.py`
- `static/war-room-ui.js`
- `static/war-room.html`
- `tests/test_production_authorization_wiring.py`
- `tests/test_war_room_actions.py`
- `tests/test_war_room_fast_gateway_corrections.py`
- `tests/test_war_room_orchestration_api.py`
- `war_room.py`
- `war_room_actions.py`
- `war_room_adapter.py`
- `war_room_authorization.py`
- `war_room_fast_gateway.py`
- `war_room_runtime.py`
- `war_room_worker.py`

New source/test files represented:

- `tests/test_war_room_ui_integrated_fix.py`
- `war_room_agents.py`

The live-only untracked `05_QA_EVIDENCE/e2e_test_only/` directory was excluded.

## Known incomplete items and verification provenance

- Agent opinion request is currently intentionally **not implemented**; the live
  UI shows it disabled and labels it `미구현` instead of reporting a fake request.
- The latest browser UAT report available during collection was
  `/home/plachem-sever/.openclaw/agents/ERPqa/05_QA_EVIDENCE/WAR-ROOM-INTEGRATED-FIX-20260907/FINAL_POSTFIX_BROWSER_UAT.md`.
  It records actual Chrome/Playwright flows and a `461 passed` regression result.
  That runtime evidence is not copied into this source-only branch.
- This task did not rerun tests or perform product QA. Prior test/UAT statements
  are provenance only and are not re-certified by the source upload.
- Source upload success must not be interpreted as deployment, service rollout,
  or a product QA verdict.

## Snapshot method

1. A separate clone was made outside the live service directory.
2. The explicit include list was hashed in the live tree before and after copy.
3. Copy was accepted only when the two live hash manifests were identical.
4. The snapshot copy was hashed and compared against the stable live manifest.
5. Secret-pattern and excluded-path scans were run before commit.
6. ERPqa independently verifies the pushed commit against the live source hashes.
