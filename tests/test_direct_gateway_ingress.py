from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import fast_gateway_api as api
from direct_gateway_authorization import CompositeGrantAuthorizer, DirectIngressGrantAuthorizer
from plachem_fast_gateway.auth_broker import AuthBrokerError, execution_auth_scope
from plachem_fast_gateway.runtime_policy import normalize_goal_contract


def scope(run_id: str = "direct-run-1", message: str = "safe"):
    return execution_auth_scope(
        agent_id="secretary",
        message=message,
        core_run_id=run_id,
        idempotency_key="direct:key",
        goal_contract=normalize_goal_contract(None).as_dict(),
    )


class FakeBroker:
    def issue(self, _scope, *, ttl_seconds, created_by):
        assert ttl_seconds == 30
        assert created_by == "direct-owner-ingress"
        return SimpleNamespace(token="one-use-token")


def test_direct_authorization_is_exact_and_one_use():
    authorizer = DirectIngressGrantAuthorizer()
    expected = scope()
    authorizer.register("direct-run-1", expected)
    with authorizer.authorize(FakeBroker(), expected) as granted:
        assert granted == (expected, "one-use-token")
    with pytest.raises(AuthBrokerError, match="AUTH_REQUIRED"):
        with authorizer.authorize(FakeBroker(), expected):
            pass


def test_direct_authorization_rejects_changed_scope_and_consumes_pending():
    authorizer = DirectIngressGrantAuthorizer()
    authorizer.register("direct-run-1", scope())
    with pytest.raises(AuthBrokerError, match="BINDING_MISMATCH"):
        with authorizer.authorize(FakeBroker(), scope(message="changed")):
            pass
    with pytest.raises(AuthBrokerError, match="AUTH_REQUIRED"):
        with authorizer.authorize(FakeBroker(), scope()):
            pass


def test_composite_routes_direct_namespace_only_once():
    direct = DirectIngressGrantAuthorizer()
    other = SimpleNamespace(owns_run=lambda run_id: run_id.startswith("war-"))
    composite = CompositeGrantAuthorizer(other, direct)
    assert composite.owns_run("direct-1") is True
    assert composite.owns_run("war-1") is True
    assert composite.owns_run("other-1") is False
    assert composite.direct() is direct


class FakeDirectAuthorizer:
    def __init__(self):
        self.registered = []

    def register(self, run_id, auth_scope):
        self.registered.append((run_id, auth_scope))


class FakeEngine:
    def __init__(self):
        self.direct = FakeDirectAuthorizer()
        self.grant_authorizer = SimpleNamespace(direct=lambda: self.direct)
        self.dispatched = []

    def dispatch(self, **kwargs):
        self.dispatched.append(kwargs)
        return {
            "core_run_id": kwargs["core_run_id"],
            "agent_id": kwargs["agent_id"],
            "status": "PASS",
            "result": {"status": "completed", "summary": "done"},
        }


def test_direct_endpoint_requires_dedicated_secret(monkeypatch):
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_INGRESS_SECRET", "expected")
    request = api.DirectDispatchRequest(
        agent_id="secretary", message="do work",
        source_run_id="source-run", source_session_key="agent:secretary:main",
    )
    with pytest.raises(HTTPException) as denied:
        api.direct_dispatch_and_wait(request, "wrong")
    assert denied.value.status_code == 401


def test_direct_endpoint_registers_exact_scope_and_dispatches(monkeypatch):
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_INGRESS_SECRET", "expected")
    engine = FakeEngine()
    monkeypatch.setattr(api, "_engine", lambda: engine)
    request = api.DirectDispatchRequest(
        agent_id="secretary", message="do work",
        source_run_id="source-run", source_session_key="agent:secretary:main",
    )
    result = api.direct_dispatch_and_wait(request, "expected")
    assert result["status"] == "PASS"
    assert result["result"]["summary"] == "done"
    assert len(engine.direct.registered) == 1
    assert len(engine.dispatched) == 1
    assert engine.dispatched[0]["timeout_seconds"] == 300
    assert engine.dispatched[0]["core_run_id"].startswith("direct-")
    assert "Return exactly one raw JSON object" in engine.dispatched[0]["message"]


def test_direct_endpoint_never_delegates_main(monkeypatch):
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("PLACHEM_FAST_GATEWAY_INGRESS_SECRET", "expected")
    request = api.DirectDispatchRequest(
        agent_id="main", message="do work",
        source_run_id="source-run", source_session_key="agent:main:main",
    )
    with pytest.raises(HTTPException) as denied:
        api.direct_dispatch_and_wait(request, "expected")
    assert denied.value.status_code == 409
    assert denied.value.detail == "MAIN_DIRECT_SESSION_NOT_DELEGATED"
