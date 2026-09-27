# War Room Auth Entry Fix — QA Result

Date: 2026-09-27

## Incident

After the Project Document Registry deployment, the War Room HTML was reachable from the existing AI Server Monitor port 8088, but the browser requested /api/war-room/projects directly against 8088 without the authenticated War Room proxy.

Observed live requests from the user's client:
- GET /api/war-room/projects -> 401 Unauthorized

The authenticated proxy service on 127.0.0.1:8113 had also stopped when plachem-ai-server-monitor.service was stopped during deployment and did not automatically restart.

## Fix

- Direct /war-room and /static/war-room.html entry redirects to PLACHEM_WAR_ROOM_EXTERNAL_URL when the request is not already authenticated by the trusted proxy.
- Query string is preserved through redirect.
- Trusted proxy requests are not redirected.
- Session cookie Secure flag respects X-Forwarded-Proto=https.
- Production external URL configured as https://openclaw.tail8eba4b.ts.net:10000/war-room.
- plachem-ai-server-monitor.service now Wants plachem-war-room-auth-proxy.service.
- plachem-war-room-auth-proxy.service is PartOf plachem-ai-server-monitor.service.

## QA

- Focused auth-entry tests: 2 passed.
- Full War Room regression: 219 passed, 7 warnings, 5 subtests passed.
- Warnings are existing FastAPI/TestClient deprecations.
