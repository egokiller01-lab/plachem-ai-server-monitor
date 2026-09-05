"""Tracked Ubuntu production composition root for Fast Gateway."""

from __future__ import annotations

from pathlib import Path

from .core_engine import AgentRegistry, CoreEngine, RunRegistry
from .runtime_policy import ModelRegistry
from .ubuntu_transport import create_ubuntu_worker_transport


def create_ubuntu_core_engine(
    *, runs_path: str | Path, agents_path: str | Path,
    models_path: str | Path, bindings_path: str | Path,
) -> CoreEngine:
    """Compose policy registries with the fixed Ubuntu OpenClaw transport."""

    transport = create_ubuntu_worker_transport(bindings_path)
    return CoreEngine(
        RunRegistry(runs_path),
        AgentRegistry.load(agents_path),
        ModelRegistry.load(models_path),
        transport,
    )
