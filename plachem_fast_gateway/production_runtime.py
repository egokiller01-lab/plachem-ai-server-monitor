"""Tracked Ubuntu production composition root for Fast Gateway."""

from __future__ import annotations

from pathlib import Path

from .core_engine import AgentRegistry, CoreEngine
from .durable_core_store import DurableCoreStore
from .runtime_policy import ModelRegistry
from .ubuntu_transport import create_ubuntu_worker_transport


def create_ubuntu_core_engine(
    *, core_db_path: str | Path | None = None, runs_path: str | Path | None = None, agents_path: str | Path,
    models_path: str | Path, bindings_path: str | Path,
) -> CoreEngine:
    """Compose policy registries with the fixed Ubuntu OpenClaw transport."""

    database = Path(core_db_path or runs_path or "runtime/fast-gateway-core.sqlite3")
    transport = create_ubuntu_worker_transport(bindings_path)
    return CoreEngine(
        DurableCoreStore(database),
        AgentRegistry.load(agents_path),
        ModelRegistry.load(models_path),
        transport,
    )
