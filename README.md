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
