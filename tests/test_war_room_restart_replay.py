"""Restart and immutable-input replay tests; no external model or production state."""
import sqlite3
import time
from threading import Event
import pytest
from plachem_fast_gateway.openclaw_adapter import OpenClawAdapter, SQLiteRunBindingStore, MemoryRunBindingStore, CoreRunStatus, AdapterOutcome
from plachem_fast_gateway.core_engine import production_result_validator
from plachem_fast_gateway.validation_state import ValidationStateStore
from war_room_actions import _grounded_instruction
from war_room_task_contract import profile_grounding
from test_war_room_task_contract_v2 import WorkerRPC
from test_fast_gateway_core_poll_regression import make_engine, Clock, BoundedTimeoutAdapter
from plachem_fast_gateway.runtime_policy import RuntimeClass
from fast_gateway_service import PersistentExecutionHarness


class Secret:
    def resolve(self):
        return "isolated-test-only"


def adapter_at(path, rpc):
    adapter = OpenClawAdapter(Secret(), SQLiteRunBindingStore(path), result_validator=production_result_validator())
    adapter.rpc = rpc
    return adapter


def submit(adapter):
    packet = profile_grounding({'task_profile': 'ACKNOWLEDGEMENT'}, {})
    message = _grounded_instruction('Do not use tools. Return SIMPLE_UI_TEST_PASS.', packet, 'FAST_GATEWAY')
    return adapter.submit('restart-test', {'agentId':'erpmanager', 'idempotencyKey':'restart-test', 'message':message, 'timeout':3600,
                                         '_trustedValidationContext':{'approved_paths':[]}})


def test_restart_restores_original_profile_and_does_not_resubmit(tmp_path):
    rpc = WorkerRPC('SIMPLE_UI_TEST_PASS')
    first = adapter_at(tmp_path/'bindings.sqlite3', rpc)
    submit(first)
    restarted = adapter_at(tmp_path/'bindings.sqlite3', rpc)
    outcome = restarted.wait('restart-test', timeout_seconds=1)
    assert outcome.status == CoreRunStatus.PASS, outcome.reason
    assert len(rpc.submitted) == 1
    assert restarted.validation_state.get('restart-test','context')['war_room_task_profile'] == 'ACKNOWLEDGEMENT'
    original = restarted.bindings.get('restart-test')
    replay = restarted.revalidate_result('restart-test')
    assert replay.status == CoreRunStatus.PASS and replay.result == outcome.result
    assert restarted.bindings.get('restart-test') == original
    assert len(rpc.submitted) == 1


def test_result_replay_keeps_original_fail_binding_and_uses_same_input(tmp_path):
    rpc = WorkerRPC('SIMPLE_UI_TEST_PASS')
    first = adapter_at(tmp_path/'bindings.sqlite3',rpc)
    submit(first)
    from plachem_fast_gateway.openclaw_adapter import ValidationDecision
    first.result_validator = lambda payload: ValidationDecision(CoreRunStatus.FAIL,'EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED')
    original = first.wait('restart-test',timeout_seconds=1)
    assert original.status == CoreRunStatus.FAIL
    restarted = adapter_at(tmp_path/'bindings.sqlite3',rpc)
    replay = restarted.revalidate_result('restart-test')
    assert replay.status == CoreRunStatus.PASS
    assert restarted.bindings.get('restart-test').status == CoreRunStatus.FAIL
    assert len(rpc.submitted) == 1


def test_validation_context_cannot_be_replaced_or_tampered(tmp_path):
    path=tmp_path/'state.sqlite3'; store=ValidationStateStore(path)
    store.put('a','context',{'war_room_task_profile':'READ_ONLY'})
    with pytest.raises(ValueError,match='CONFLICT'):
        store.put('a','context',{'war_room_task_profile':'CODE_CHANGE'})
    with sqlite3.connect(path) as con:
        con.execute("UPDATE gateway_validation_state SET payload='{}' WHERE core_run_id='a'")
    with pytest.raises(ValueError,match='TAMPERED'):
        store.get('a','context')


def test_missing_snapshot_is_not_a_success_or_a_resubmission(tmp_path):
    rpc=WorkerRPC('SIMPLE_UI_TEST_PASS');adapter=adapter_at(tmp_path/'bindings.sqlite3',rpc)
    submit(adapter)
    with pytest.raises(ValueError,match='SNAPSHOT_UNAVAILABLE'):
        adapter.revalidate_result('restart-test')
    assert len(rpc.submitted)==1


def test_observer_reconnects_without_new_agent_submission():
    ready=Event(); complete=Event(); engine=make_engine(RuntimeClass.LOCAL,Clock())
    class Adapter(BoundedTimeoutAdapter):
        def __init__(self):
            super().__init__();self.bindings=MemoryRunBindingStore();self.submissions=0
        def submit(self,*args):
            self.submissions+=1;binding=super().submit(*args);self.bindings.put(binding);return binding
        def wait(self,*args,**kwargs):
            ready.set()
            return AdapterOutcome(CoreRunStatus.PASS if complete.is_set() else CoreRunStatus.RUNNING,'observed')
    engine.adapter=Adapter()
    engine.dispatch(agent_id='worker',message='test',timeout_seconds=1,core_run_id='restart-observer',watchdog_managed=True)
    harness=PersistentExecutionHarness(engine)
    try:
        harness.resume_observation('restart-observer'); harness.resume_observation('restart-observer')
        assert ready.wait(1)
        assert len(harness.active_controllers)==1
        complete.set()
        for _ in range(100):
            if engine.status('restart-observer')['status']=='PASS': break
            time.sleep(.01)
        assert engine.status('restart-observer')['status']=='PASS'
        assert engine.adapter.submissions==1
    finally:
        harness.close()


def test_restart_between_result_collection_and_core_commit_preserves_payload(tmp_path):
    rpc=WorkerRPC('SIMPLE_UI_TEST_PASS'); first=adapter_at(tmp_path/'bindings.sqlite3',rpc)
    submit(first); original=first.wait('restart-test',timeout_seconds=1)
    restarted=adapter_at(tmp_path/'bindings.sqlite3',rpc)
    restored=restarted.wait('restart-test',timeout_seconds=1)
    assert restored==original
    assert restored.result['summary']=='SIMPLE_UI_TEST_PASS'
    assert len(rpc.submitted)==1


def test_restored_failure_cannot_launch_an_automatic_recovery():
    class Registry:
        def recover(self,*args):
            raise AssertionError('restart must not create a recovery execution')
    class Core:
        adapter=None
        def status(self,run_id):
            return {'core_run_id':run_id,'status':'FAIL'}
    harness=PersistentExecutionHarness(Core()); harness.set_recovery_layer(Registry())
    harness._restored_runs.add('restored')
    try:
        harness._handle_recovery({'core_run_id':'restored','status':'FAIL'})
    finally:
        harness.close()


def test_saved_pass_never_overrides_cancelled_binding(tmp_path):
    from dataclasses import replace
    rpc=WorkerRPC('SIMPLE_UI_TEST_PASS');adapter=adapter_at(tmp_path/'bindings.sqlite3',rpc)
    submit(adapter);adapter.wait('restart-test',timeout_seconds=1)
    adapter.bindings.put(replace(adapter.bindings.get('restart-test'),status=CoreRunStatus.CANCELLED))
    restarted=adapter_at(tmp_path/'bindings.sqlite3',rpc)
    assert restarted.wait('restart-test',timeout_seconds=1).status==CoreRunStatus.CANCELLED
