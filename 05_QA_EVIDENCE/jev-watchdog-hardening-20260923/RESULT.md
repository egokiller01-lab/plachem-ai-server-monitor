# JEV Watchdog Hardening — 2026-09-23

## Scope
- General OpenClaw Agent Watchdog
- JEV guarded SALVAGE/recovery phases
- FastGateway JEV Session Watchdog
- Monitor/API integration
- No Git commit/push/merge

## Fixed
- General grace clock now uses sessionStartedAt; lastInteractionAt no longer resets the 5-minute grace.
- :jev-handoff: sessions are control-plane and excluded from JEV polling.
- :jev-recovery: sessions remain observable but cannot spawn another automatic recovery.
- PERIODIC_HEALTH SALVAGE requires 2 consecutive confirmations with the same stable state.
- Any existing recovery attempt for the same source blocks duplicate automatic recovery.
- Recovery target session key is deterministic per source session.
- If source finishes while handoff is being written, recovery restart is cancelled.
- Restart is allowed only after a confirmed source abort.

## FastGateway
- Existing 5-minute / 60-second timing already used run started_at.
- RESTART_SESSION now requires two consecutive JEV confirmations.
- Watchdog recovery children are capped at generation 1; grandchildren are rejected.

## Verification
- Focused/general/FastGateway/Monitor regression: 147 passed, 38 subtests passed.
- Services restarted successfully:
  - plachem-ai-server-monitor.service active/running
  - plachem-jev-agent-watchdog.service active/running
- /api/status HTTP 200
- /internal/jev-recovery-live-status HTTP 200, inflight=[]
- /api/detail/agent-watchdog HTTP 200, available=True, abnormal_count=0
- Running OpenClaw sessions at post-deploy check: 0

## Backup
- /home/plachem-sever/openclaw-backups/jev-watchdog-hardening-20260923-2317
