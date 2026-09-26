# JEV Watchdog Single-Entry Hardening — 2026-09-24

## Verdict
PASS — code regression + deployed service state.

## Architecture
- agent-stall-detector: evidence collection/persistence only; no recovery endpoint forwarding.
- General Watchdog supervisor: sole production JEV recovery decision entry.
- Policy: session start + 5 minutes, then ~60 second cadence.
- Handoff is deterministic code-written RECOVERY_HANDOFF.md; no handoff Agent session.
- SALVAGE requires 2 spaced confirmations on the same stable_state_digest.
- State change resets SALVAGE confirmation to 1.
- Only PERIODIC_HEALTH from source=periodic_supervisor may enter auto recovery.
- Main SALVAGE escalates; Main is never automatically recovered.
- Recovery sessions may be observed but cannot create a second recovery generation.
- One source session may have at most one recovery attempt, with deterministic target key.
- Terminal/no-active source is rechecked before abort/restart.
- Automatic recovery has a persistent systemd kill switch.

## Deployment
- plachem-ai-server-monitor.service: active
- plachem-jev-agent-watchdog.service: active
- plachem-agent-stall-detector.timer: active
- PLACHEM_JEV_AUTO_RECOVERY_ENABLED=1 loaded by monitor systemd drop-in.
- Post-enable running OpenClaw sessions: 0
- Recovery inflight: []
- No test Agent sessions were created during this hardening pass.

## Tests
- Focused Watchdog/FastGateway: 120 passed + 38 subtests
- agent-stall-detector: 14 passed
- Full relevant regression: 156 passed, 4 FastAPI deprecation warnings, 38 subtests
- detector forwarding probe: eligible=2, sent=0, accepted=0, failed=0, mode=evidence_only

## Backup
/home/plachem-sever/openclaw-backups/20260924-watchdog-single-entry

## Notes
- Existing historical FastGateway FG_STALL rows remain detector evidence only; they do not start General Watchdog recovery.
- No synthetic/live QwenTest recovery session was launched in this pass. Future real stalls will exercise the deployed recovery path under the guarded policy.
