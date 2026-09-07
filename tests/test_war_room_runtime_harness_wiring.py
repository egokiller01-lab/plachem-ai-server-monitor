"""Exercise the real War Room composition with an in-memory OpenClaw boundary."""

import sqlite3
from threading import Event

import pytest

import fast_gateway_service as service
import war_room
import war_room_actions
from fastapi.testclient import TestClient
from test_production_authorization_wiring import production, prepared
from war_room_runtime import WarRoomRuntime


class Legacy:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


@pytest.fixture
def composition(production):
    root, _, owner, _ = production
    legacy = Legacy()
    runtime = WarRoomRuntime(adapter=legacy)
    yield runtime, legacy, [owner], root
    runtime.close()


def deliver(runtime, tmp_path, delivery_id="delivery-1"):
    from app import app
    client = TestClient(app)
    try:
        item = prepared((tmp_path, client, None, None), key=delivery_id)
    finally:
        client.close()
    delivery = item["deliveries"][0]
    with sqlite3.connect(tmp_path / "war-room.sqlite3") as db:
        db.execute("UPDATE war_deliveries SET id=? WHERE id=?", (delivery_id, delivery["delivery_id"]))
    adapter = runtime.adapter_for("FAST_GATEWAY", tmp_path / "war-room.sqlite3")
    receipt = adapter.deliver(delivery_id=delivery_id, agent_id="ERPcoder",
                              instruction_id=item["message_id"], body=item["body"])
    assert receipt.status == "received"
    return adapter


def test_delivery_completes_without_caller_wait_or_poll(composition):
    runtime, _, transports, tmp_path = composition
    completed = Event()
    harness = service.get_persistent_harness()
    harness.set_terminal_completion_subscriber(lambda record: completed.set())
    adapter = deliver(runtime, tmp_path)
    owner = transports[0]
    owner.finished.add("war-delivery-1")
    owner.wake.set()
    assert completed.wait(1.0), "runtime delivery never reached the Harness observer"
    assert adapter.execution_snapshot("delivery-1")["run_status"] == "PASS"
    assert harness.active_controllers == {}
    assert len(transports) == 1


@pytest.mark.parametrize("first", ["runtime", "orchestrator"])
def test_phase2_and_delivery_share_owner_and_targeted_stop(composition, first):
    runtime, _, transports, tmp_path = composition
    if first == "runtime":
        adapter = deliver(runtime, tmp_path)
        orchestrator = war_room_actions._execution_orchestrator()
    else:
        orchestrator = war_room_actions._execution_orchestrator()
        adapter = deliver(runtime, tmp_path)
    assert adapter.engine is orchestrator.core_engine
    assert len(transports) == 1
    deliver(runtime, tmp_path, "delivery-2")
    stopped = orchestrator.stop_controller.stop_core_run(core_run_id="war-delivery-1")
    assert stopped.status == "stopped"
    assert adapter.execution_snapshot("delivery-1")["run_status"] == "CANCELLED"
    assert adapter.execution_snapshot("delivery-2")["run_status"] == "RUNNING"
    assert transports[0].aborts == [{"key": "agent:erpcoder:war-delivery-1"}]


def test_runtime_close_preserves_shared_phase2_owner(composition):
    runtime, legacy, transports, tmp_path = composition
    harness = service.get_persistent_harness()
    runtime.adapter_for("FAST_GATEWAY", tmp_path / "war-room.sqlite3")
    runtime.close()
    assert legacy.close_calls == 1
    assert all(transport.close_calls == 0 for transport in transports)
    assert runtime.adapter_for("FAST_GATEWAY", tmp_path / "war-room.sqlite3").engine is harness.engine


def test_application_shutdown_closes_harness_when_runtime_disabled(composition):
    from app import stop_war_room_runtime
    service.get_persistent_harness()
    stop_war_room_runtime()
    _, _, transports, _ = composition
    assert len(transports) == 1
    assert transports[0].close_calls == 1
