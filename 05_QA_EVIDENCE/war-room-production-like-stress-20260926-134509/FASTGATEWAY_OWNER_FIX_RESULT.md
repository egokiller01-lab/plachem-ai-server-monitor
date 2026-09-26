# FastGateway OpenClaw owner/cancel fix — 2026-09-26

## Verdict
PASS — the FastGateway/OpenClaw adapter regression was isolated, corrected, and verified.

## Root cause
- `OpenClawAdapter._resolve_session_id()` performed optional session-id enrichment through `self.rpc.request("sessions.list")` on the persistent run connection.
- An unsupported or failed auxiliary lookup could trigger the persistent client's reconnect/disconnect path, clearing the live socket and negotiated methods after an agent run had already been accepted.
- Later cancel/abort then lost the preferred `sessions.abort` negotiation and could fall through to `chat.abort` with `key` instead of the required `sessionKey`.
- One separate test expected the pre-security worker payload and had not included the internal `_trustedValidationContext` field.

## Fix
- Session-id enrichment now uses a private `sessions.describe(key)` RPC.
- Session-id lookup failure is best-effort and cannot alter the persistent run socket.
- Normal cancel prefers the current persistent connection.
- On persistent-connection transport loss, cancel performs exactly one private same-device abort attempt.
- The private abort connection renegotiates methods and prefers `sessions.abort`; `chat.abort` fallback uses `sessionKey` correctly.
- `_trustedValidationContext` remains internal; the external FastGateway API request models use `extra="forbid"` and do not expose this field.

## Verification
- Original failing OpenClaw/transport subset after fix: 55 passed + 7 subtests.
- FastGateway full lower-level regression: 326 passed + 62 subtests.
- War Room/FastGateway integration regression: 174 passed + 5 subtests.
- Python compileall: PASS.
- git diff --check on touched files: PASS.
- Live OpenClaw Gateway advertises `sessions.abort`, `sessions.describe`, `sessions.list`, and `chat.abort`.
- Live canary 1: owner connection preserved through session-id lookup; owner `sessions.abort` returned CANCELLED.
- Live canary 2: owner socket deliberately dropped after submit; same stable device-id fresh abort returned CANCELLED.
- AI Server Monitor, War Room auth proxy, JEV Watchdog, and OpenClaw Gateway are active after reload.

## Safety
- No production task data was modified by the live canaries.
- Canary sessions were disposable and cleanup was attempted after each case.
- No Git commit or push was performed.
