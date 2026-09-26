# War Room Timeout 3600 + Auto QA — 2026-09-25

## Verdict
PASS — deployed and live E2E verified.

## Changes
- Direct War Room OpenClaw worker timeout: 300s -> 3600s.
- Added PLACHEM_WAR_ROOM_DIRECT_TIMEOUT_SECONDS=3600.
- Added PLACHEM_WAR_ROOM_AUTO_QA=1.
- Worker completion now transitions to QA and automatically queues the independent reviewer.
- QA always runs through a direct disposable read-only session, even when the Worker used Fast Gateway.
- Worker evidence is materialized into the existing QA evidence contract.
- Auto QA PASS/FAIL/REWORK is recorded in war_qa_verdicts using existing signature/evidence validation.
- QA execution does not consume Worker call/turn budget.
- Representative final-completion approval remains unchanged.

## Tests
- War Room/adapter focused regression: 11 passed.
- Watchdog/FastGateway regression: 120 passed + 38 subtests.
- Isolated SQLite + fake adapter E2E: PASS.
- Live OpenClaw disposable-session E2E:
  - ERPmanager worker: responded
  - task: running -> qa
  - ERPqa reviewer: auto queued -> responded
  - QA verdict: PASS
  - Worker budget: call_count=1, turn_count=1
  - immutable evidence captured
  - no running test sessions after completion

## Live E2E
Worker session: 4eb0594e-91ee-4ea9-945f-2f1edc1336d5
QA session: 7573ebeb-069e-4484-9e5e-02fbf97b279b
Report:
/home/plachem-sever/.openclaw/agents/ERPmanager/01_ACTIVE/war-room-autoqa-live-20260925-230244/LIVE_AUTO_QA_REPORT.md

## Operational state
- plachem-ai-server-monitor.service: active
- plachem-jev-agent-watchdog.service: active
- plachem-agent-stall-detector.timer: active
- plachem-context-index-v3.timer: active
- PLACHEM_WAR_ROOM_DIRECT_TIMEOUT_SECONDS=3600
- PLACHEM_WAR_ROOM_AUTO_QA=1
- PLACHEM_JEV_AUTO_RECOVERY_ENABLED=1

## Safety
- Existing human representative approval gate is unchanged.
- Existing production work sessions were not used.
- Test-only OpenClaw sessions were disposable.
- No Git push/merge/deploy was performed.
