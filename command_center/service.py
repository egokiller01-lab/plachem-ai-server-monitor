from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path
from wsgiref.simple_server import make_server

COMMAND_CENTER_DIR = Path(__file__).resolve().parent
if str(COMMAND_CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(COMMAND_CENTER_DIR))

from agent_registry import AgentRegistry
from http_api import create_app
from war_room_orchestrator import WarRoomOrchestrator
from workspace_registry import WorkspaceRegistry

ROOT = Path(__file__).resolve().parents[1]


def build_app():
    """Build the gated local WSGI service from server-side configuration."""
    if os.environ.get("PLACHEM_COMMAND_CENTER_WAR_ROOM_API") != "1":
        raise RuntimeError("Command Center War Room API feature gate is off")
    agent_path = Path(os.environ.get("PLACHEM_COMMAND_CENTER_AGENTS", ROOT / "plachem_fast_gateway" / "agents.json"))
    workspace_path = Path(os.environ.get("PLACHEM_COMMAND_CENTER_WORKSPACES", Path(__file__).with_name("workspaces.json")))
    run_path = Path(os.environ.get("PLACHEM_COMMAND_CENTER_RUNS", ROOT / "runtime" / "runs.jsonl"))
    idem_path = Path(os.environ.get("PLACHEM_COMMAND_CENTER_IDEMPOTENCY", ROOT / "runtime" / "command-center-idempotency.json"))
    secret = os.environ.get("PLACHEM_WAR_ROOM_INTEGRATION_SECRET")
    if not secret:
        raise RuntimeError("PLACHEM_WAR_ROOM_INTEGRATION_SECRET is required")
    agents = AgentRegistry.load(agent_path)
    workspaces = WorkspaceRegistry.load(workspace_path)
    orchestrator = WarRoomOrchestrator(agents, workspaces, run_registry_path=run_path)
    return create_app(orchestrator, idempotency_path=idem_path, integration_secret=secret, enabled=True)


def _loopback_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("Command Center service host must be a loopback IP") from exc
    if not address.is_loopback:
        raise ValueError("Command Center service host must be a loopback IP")
    return value


def _service_port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError("Command Center service port must be between 1 and 65535")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Command Center War Room API")
    parser.add_argument("--host", default=os.environ.get("PLACHEM_COMMAND_CENTER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PLACHEM_COMMAND_CENTER_PORT", "8790")))
    args = parser.parse_args()
    host = _loopback_host(args.host)
    port = _service_port(args.port)
    with make_server(host, port, build_app()) as server:
        print(f"Command Center War Room API listening on {host}:{port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
