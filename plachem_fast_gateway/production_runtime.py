"""Tracked Ubuntu production composition root for Fast Gateway."""

from __future__ import annotations

from pathlib import Path
import os

from .core_engine import AgentRegistry, CoreEngine
from .core_engine import RunRegistry
from .runtime_policy import ModelRegistry
from .ubuntu_transport import create_ubuntu_worker_transport
from .auth_broker import create_production_auth_broker


def create_ubuntu_core_engine(
    *, core_db_path: str | Path | None = None, runs_path: str | Path | None = None, agents_path: str | Path,
    models_path: str | Path | None = None, bindings_path: str | Path,
) -> CoreEngine:
    """Compose agent admission with the fixed Ubuntu OpenClaw transport.

    models_path is retained for compatibility, but model configuration belongs
    to OpenClaw and cannot be a prerequisite for this composition root.
    """

    database = Path(runs_path or core_db_path or "runtime/fast-gateway-runs.jsonl")
    transport = create_ubuntu_worker_transport(bindings_path)
    # Read-only composition is possible without credentials, but Core itself
    # must deny dispatch without the broker, including non-HTTP callers.
    auth_broker = (
        create_production_auth_broker()
        if os.environ.get("PLACHEM_AUTH_BROKER_DB") and os.environ.get("PLACHEM_AUTH_BROKER_KEY_ID")
        else None
    )
    return CoreEngine(
        RunRegistry(database),
        AgentRegistry.load(agents_path),
        ModelRegistry({}),
        transport,
        auth_broker=auth_broker,
        auth_required=True,
    )
