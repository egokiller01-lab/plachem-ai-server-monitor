"""One-use authorization for owner-originated direct agent turns.

The OpenClaw ingress plugin authenticates to the monitor over loopback and
registers one exact execution scope immediately before Core dispatch.  The
registration is memory-only, short-lived, and consumed once; a service restart
therefore fails closed instead of preserving stale direct-run authority.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from plachem_fast_gateway.auth_broker import (
    AuthBrokerError,
    AuthScope,
    SQLiteAuthBroker,
    canonical_task_digest,
)


@dataclass(frozen=True)
class _PendingDirectGrant:
    task_digest: str
    expires_at_ms: int


class DirectIngressGrantAuthorizer:
    """Authorize exactly one registered ``direct-*`` Core run."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingDirectGrant] = {}
        self._lock = threading.RLock()

    @staticmethod
    def owns_run(core_run_id: str) -> bool:
        return core_run_id.startswith("direct-")

    def register(self, core_run_id: str, scope: AuthScope, *, ttl_seconds: int = 30) -> None:
        if not self.owns_run(core_run_id) or ttl_seconds <= 0:
            raise AuthBrokerError("AUTH_REQUIRED")
        pending = _PendingDirectGrant(
            task_digest=canonical_task_digest(scope),
            expires_at_ms=int(time.time() * 1000) + ttl_seconds * 1000,
        )
        with self._lock:
            self._prune_locked()
            existing = self._pending.get(core_run_id)
            if existing is not None and existing.task_digest != pending.task_digest:
                raise AuthBrokerError("CONFLICT")
            self._pending[core_run_id] = pending

    @contextmanager
    def authorize(self, broker: SQLiteAuthBroker, scope: AuthScope):
        run_id = str(scope.task_contract.get("core_run_id") or "")
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._prune_locked(now_ms)
            pending = self._pending.pop(run_id, None)
        if pending is None:
            raise AuthBrokerError("AUTH_REQUIRED")
        if pending.expires_at_ms <= now_ms:
            raise AuthBrokerError("EXPIRED")
        if pending.task_digest != canonical_task_digest(scope):
            raise AuthBrokerError("BINDING_MISMATCH")
        grant = broker.issue(scope, ttl_seconds=30, created_by="direct-owner-ingress")
        yield scope, grant.token

    def _prune_locked(self, now_ms: int | None = None) -> None:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        for run_id in [key for key, value in self._pending.items() if value.expires_at_ms <= current]:
            self._pending.pop(run_id, None)


class CompositeGrantAuthorizer:
    """Route Core authorization to the owner of each run namespace."""

    def __init__(self, *authorizers: object) -> None:
        self.authorizers = tuple(authorizers)

    def owns_run(self, core_run_id: str) -> bool:
        return any(bool(authorizer.owns_run(core_run_id)) for authorizer in self.authorizers)

    def direct(self) -> DirectIngressGrantAuthorizer:
        for authorizer in self.authorizers:
            if isinstance(authorizer, DirectIngressGrantAuthorizer):
                return authorizer
        raise AuthBrokerError("AUTH_REQUIRED")

    @contextmanager
    def authorize(self, broker: SQLiteAuthBroker, scope: AuthScope):
        run_id = str(scope.task_contract.get("core_run_id") or "")
        matches = [authorizer for authorizer in self.authorizers if authorizer.owns_run(run_id)]
        if len(matches) != 1:
            raise AuthBrokerError("AUTH_REQUIRED")
        with matches[0].authorize(broker, scope) as authorized:
            yield authorized
