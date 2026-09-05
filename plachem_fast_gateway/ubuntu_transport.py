"""Ubuntu OpenClaw worker transport bootstrap.

This module contains no service, HTTP, authorization, or workspace policy.  It
only constructs the default transport used after those checks have succeeded.
"""

from __future__ import annotations

from pathlib import Path

from .openclaw_adapter import EnvironmentSecretRef, OpenClawAdapter, SQLiteRunBindingStore
from .worker_transport import WorkerTransport


def create_ubuntu_worker_transport(bindings_path: str | Path) -> WorkerTransport:
    """Build the fixed local OpenClaw transport without caller runtime overrides."""

    # Local import avoids coupling the transport protocol to Core construction.
    from .core_engine import production_result_validator

    return OpenClawAdapter(
        EnvironmentSecretRef(),
        SQLiteRunBindingStore(bindings_path),
        result_validator=production_result_validator(),
    )
