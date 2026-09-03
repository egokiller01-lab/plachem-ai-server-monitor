from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from command_center_client import CommandCenterAPIError, CommandCenterClient
from war_room import _request_principal
from war_room_execution import COMMAND_CENTER_MODE, execute_war_room_task

router = APIRouter(prefix="/api/war-room/command-center")


def _client(request: Request) -> CommandCenterClient:
    principal = _request_principal(request, None, None)
    if not principal:
        raise PermissionError("War Room authentication required")
    secret = os.environ.get("PLACHEM_WAR_ROOM_INTEGRATION_SECRET", "")
    if not secret:
        raise RuntimeError("Command Center integration is unavailable")
    return CommandCenterClient(
        os.environ.get("PLACHEM_COMMAND_CENTER_URL", "http://127.0.0.1:8790"),
        secret,
        principal,
    )


def _key(request: Request) -> str:
    return request.headers.get("Idempotency-Key", "")


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, PermissionError):
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if isinstance(exc, CommandCenterAPIError):
        return JSONResponse({"detail": f"Command Center request failed ({exc.status})"}, status_code=exc.status)
    if isinstance(exc, (ValueError, RuntimeError)):
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"detail": "Command Center unavailable"}, status_code=503)


async def _run(request: Request, operation):
    try:
        return operation()
    except Exception as exc:
        return _error(exc)


@router.post("/submit")
async def submit(request: Request):
    try:
        payload: dict[str, Any] = await request.json()
        mode = payload.pop("execution_mode", COMMAND_CENTER_MODE)
        return execute_war_room_task(mode, command_center=lambda: _client(request).submit(payload, _key(request)), legacy=lambda: {"mode": "legacy"})
    except Exception as exc:
        return _error(exc)


@router.get("/{project_id}/tasks/{task_id}")
async def status(project_id: str, task_id: str, request: Request):
    try: return _client(request).status(project_id, task_id)
    except Exception as exc: return _error(exc)


@router.get("/{project_id}/tasks/{task_id}/candidates")
async def candidates(project_id: str, task_id: str, request: Request):
    try: return _client(request).candidates(project_id, task_id)
    except Exception as exc: return _error(exc)


@router.post("/tasks/{task_id}/dispatch")
async def dispatch(task_id: str, request: Request):
    try: return execute_war_room_task(COMMAND_CENTER_MODE, command_center=lambda: _client(request).dispatch(task_id, _key(request)), legacy=lambda: {"mode": "legacy"})
    except Exception as exc: return _error(exc)


@router.get("/{project_id}/tasks/{task_id}/next-ready")
async def next_ready(project_id: str, task_id: str, request: Request):
    try: return _client(request).next_ready(project_id, task_id)
    except Exception as exc: return _error(exc)


@router.get("/{project_id}/tasks/{task_id}/summary")
async def summary(project_id: str, task_id: str, request: Request):
    try: return _client(request).summary(project_id, task_id)
    except Exception as exc: return _error(exc)
