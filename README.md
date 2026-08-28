# PLACHEM AI Server Monitor

Minimal FastAPI dashboard for monitoring the PLACHEM AI server.

## Scope

This project is self-contained in this repository:

```text
plachem-ai-server-monitor
```

It does not modify OpenClaw, ERP, Ollama, Tailscale, or systemd configuration. The included service file is only an example.

## Features

- CPU usage
- RAM usage
- Disk usage
- Network download/upload speed
- GPU name
- GPU usage
- VRAM usage
- GPU temperature
- OpenClaw status
- Ollama status
- Tailscale status
- 2 second frontend refresh
- GPU collection failure isolation

GPU data is collected from `nvidia-smi` first. If that fails, the app attempts `pynvml`. If both fail, the dashboard keeps running and shows `Unknown` or `Error` only on the GPU card.

## Run Locally

```bash
cd plachem-ai-server-monitor
python3 -m pip install --target .deps -r requirements.txt
PYTHONPATH=.deps python3 -m uvicorn app:app --host 0.0.0.0 --port 8088
```

Open:

```text
http://SERVER_IP:8088
```

## API

```text
GET /api/status
```

Returns one JSON payload with system, GPU, network, and service state.

## Agent War Room — controlled phase

The controlled War Room phase is available at:

```text
GET /war-room
```

Authenticated read APIs include:

```text
GET /api/war-room/projects
GET /api/war-room/projects/{project_id}
GET /api/war-room/projects/{project_id}/participants
GET /api/war-room/projects/{project_id}/timeline
GET /api/war-room/projects/{project_id}/operations
GET /api/war-room/projects/{project_id}/manyfast-baseline
```

Timeline reads support `message_type`, `author_id`, `delivery_status`, `from_ts`,
`to_ts`, `before`, and `limit` filters. Controlled writes require an
`Idempotency-Key`; task calls default to one call, one response turn, and a
ten-minute deadline, with explicit assignee targeting and active-call dedupe.

The default database location is `~/.openclaw/war-room/war_room.sqlite3`. Override it with
`PLACHEM_WAR_ROOM_DB` for testing or deployment. Controlled task, approval,
transition, participant, evidence, and audit endpoints require the server-side
`PLACHEM_WAR_ROOM_PRINCIPAL_TOKENS` map, a matching `X-War-Room-Token`, and
`Idempotency-Key`; the optional actor header is checked against the authenticated principal.
For browser use, the trusted reverse proxy may provide `X-Authenticated-Principal` (or
`X-Forwarded-User`) plus `X-War-Room-Proxy-Secret` on the initial `/war-room` request.
The server exchanges that verified identity for an HttpOnly signed session cookie, so
the browser does not receive or inject a War Room token. The real adapter is disabled
by default. When `PLACHEM_WAR_ROOM_REAL_ADAPTER=1` is explicitly configured, it uses
the official OpenClaw Gateway `chat.send` and `chat.abort` RPCs for the named agent's
latest stored session. `PLACHEM_WAR_ROOM_ADAPTER_COMMAND` may instead provide a reviewed
bridge executable; neither path uses a shell.

## systemd Example

The example unit is located at:

```text
systemd/plachem-ai-server-monitor.service.example
```

Example install flow:

```bash
sudo cp systemd/plachem-ai-server-monitor.service.example /etc/systemd/system/plachem-ai-server-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now plachem-ai-server-monitor
```

Do not run the install flow until the service path and local `.deps` dependencies have been verified.

Optional folder-size monitoring can be configured with:

```bash
export PLACHEM_MONITOR_FOLDERS="/srv/data,/var/log"
```
