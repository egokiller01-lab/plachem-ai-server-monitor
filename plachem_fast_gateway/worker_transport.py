"""Transport boundary used by the Fast Gateway Core.

Implementations own worker submission and lifecycle RPC details.  The Core
passes only the allow-listed OpenClaw execution fields and retains all policy,
authorization, workspace, and model selection metadata itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .openclaw_adapter import AdapterOutcome, RunBinding


@runtime_checkable
class WorkerTransport(Protocol):
    """Minimal worker lifecycle contract required by :class:`CoreEngine`."""

    def connect(self) -> Mapping[str, Any]: ...

    def submit(self, core_run_id: str, payload: Mapping[str, Any]) -> RunBinding: ...

    def wait(self, core_run_id: str, *, timeout_seconds: float) -> AdapterOutcome: ...

    def cancel(self, core_run_id: str) -> RunBinding: ...

    def close(self) -> None: ...
