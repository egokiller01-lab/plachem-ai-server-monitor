# Token Telemetry Monitor + Watchdog/JEV Integration — 2026-09-26

## Verdict
PASS — deployed.

## Monitor
- OpenClaw Control > Agents has a separate Token State column.
- Values: NORMAL / HIGH / ABNORMAL / UNAVAILABLE.
- State (WORKING/CONTINUE/WATCH/SALVAGE/DEAD/etc.) remains independent.
- Tooltip includes current prompt, context pressure, tokens/hour, calls/hour, cache ratio, 24h volume and reasons when available.

## Watchdog/JEV
- General Watchdog reads Context Index V3 token telemetry by exact agent + session_id.
- Only fresh ABNORMAL telemetry (<=15m) is appended to PERIODIC_HEALTH evidence.
- NORMAL/HIGH telemetry is not forwarded to JEV by this path.
- Token telemetry never independently triggers recovery.
- token_telemetry is excluded from stable_state_digest, so changing counters cannot reset the two-confirmation SALVAGE guard.

## Tests
- Monitor + telemetry + supervisor integration: 20 passed.
- Watchdog/FastGateway regression: 120 passed + 38 subtests.
- Monitor inline JavaScript: node --check PASS.
- API /api/openclaw/status: 11 agents expose token telemetry.
- API /api/detail/agent-watchdog: token_abnormal_count=0 at deployment time.

## Current deployment state
- plachem-ai-server-monitor.service active
- plachem-jev-agent-watchdog.service active
- plachem-agent-stall-detector.timer active
- plachem-context-index-v3.timer active

## Safety
- No automatic token-based kill/restart/throttle was added.
- Existing JEV recovery decision and two-confirmation policy remain authoritative.
