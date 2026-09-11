# FastGateway Recovery v0.1 — Live Test Evidence (2026-09-11)

## Verdict: PASS_RECOVERED

## Test Design
- Source run: newly created (existing run reuse forbidden)
- Task: read-only `hostname` query (safe, no side effects)
- Dispatch path: `create_core_engine()` factory (identical to production service)
- Auth: `DirectIngressGrantAuthorizer.register()` for source + recovery child
- Recovery: manual 1-shot dispatch via `RecoveryLayer.attempt_recovery()` — no auto-recovery wiring

## Timeline (UTC)
| Step | Time | Event |
|------|------|-------|
| 1 | 06:22:09 | Primary dispatch `direct-75000697...` → FAIL / WORKER_FAILED |
| 2 | 06:22:58 | session_id resolved via sessions.list: `9c469cce-c179-44ea-a649-a3348c87ef24` |
| 3 | 06:23:19 | Recovery dispatch `...-rec-b91494dc` → PASS |
| 4 | 06:24:06 | Final verdict: PASS_RECOVERED |

## Results (7/7)
1. Primary FAIL: WORKER_FAILED
2. session_id: 9c469cce-c179-44ea-a649-a3348c87ef24
3. Recovery auth grant: DirectIngressGrantAuthorizer.register()
4. Worker execution: dispatch_attempt=1, execution_attempt=1
5. Result reuse: hostname plachem-sever-X570-GAMING-X
6. Validator: format_error=none, attempts=0, status=completed
7. Final: PASS_RECOVERED

## Run IDs
- source: direct-75000697acb74e64832039015fdc8724
- recovery: direct-75000697acb74e64832039015fdc8724-rec-b91494dc

## Child raw response
{"status":"completed","summary":"Retrieved hostname: plachem-sever-X570-GAMING-X","evidence":[{"type":"command_output","detail":"exec `hostname` returned plachem-sever-X570-GAMING-X"}],"artifacts":[],"scope":{"compliant":true,"violations":[]}}

## FastGateway health
- service active (PID 2740490), port 8088, /api/fast-gateway/runs 200

## Known limitation (out of scope)
- child session_id null in run record (sessions.list fallback exists, commit a8edff2)
