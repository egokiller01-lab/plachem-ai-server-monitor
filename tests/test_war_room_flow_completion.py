"""Fault-injection tests across real workflow code with isolated DB/transport boundaries."""
import json
import sqlite3
import time
from pathlib import Path
from threading import Event, Thread

import pytest
import fast_gateway_service as service
import war_room_actions as actions
from war_room_execution_units import ExecutionUnitStore
from war_room_orchestration import WarRoomOrchestrator, DuplicateDispatchError
from test_war_room_orchestration import FakeCoreEngine
from test_fast_gateway_core_poll_regression import make_engine, Clock, BoundedTimeoutAdapter
from plachem_fast_gateway.runtime_policy import RuntimeClass
from plachem_fast_gateway.openclaw_adapter import OpenClawAdapter, AdapterOutcome, CoreRunStatus
from test_production_authorization_wiring import production
from test_war_room_task_contract_v2 import prepare_profiled, WorkerRPC
from war_room_runtime import WarRoomRuntime
from war_room_adapter import DeliveryReceipt
from war_room_worker import _terminal_validation_failure, process_due_deliveries, recover_received_deliveries, stop_bound_delivery


def orchestrator(tmp_path):
    db = tmp_path / 'graph.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE war_projects(id TEXT PRIMARY KEY)')
        con.execute('CREATE TABLE war_tasks(id TEXT PRIMARY KEY)')
        con.execute("INSERT INTO war_projects VALUES ('p')")
        con.execute("INSERT INTO war_tasks VALUES ('t')")
    store = ExecutionUnitStore(db)
    store.ensure_schema()
    core = FakeCoreEngine()
    return WarRoomOrchestrator(core_engine=core, execution_store=store), store, core


def test_partial_workflow_registration_rolls_back(tmp_path, monkeypatch):
    orch, store, _ = orchestrator(tmp_path)
    import war_room_execution_compiler as compiler
    compiled = compiler.compile_task_intent(war_project_id='p', war_task_id='t', agents=['A','B'])
    compiled['execution_units'][1]['execution_id'] = compiled['execution_units'][0]['execution_id']
    monkeypatch.setattr(compiler, 'compile_task_intent', lambda **kwargs: compiled)
    with pytest.raises(ValueError):
        orch.compile_and_persist(war_project_id='p', war_task_id='t', agents=['A','B'])
    assert store.get_units_by_task('p','t') == []


def test_conflicting_retry_cannot_overwrite_saved_input_after_restart(tmp_path):
    orch, store, core = orchestrator(tmp_path)
    item = orch.compile_and_persist(war_project_id='p',war_task_id='t',agents=['A'])['execution_units'][0]
    orch.dispatch_execution(execution_id=item['execution_id'],message='approved original',timeout_seconds=3600)
    restarted = WarRoomOrchestrator(core_engine=core,execution_store=store)
    with pytest.raises(DuplicateDispatchError):
        restarted.dispatch_execution(execution_id=item['execution_id'],message='different task',timeout_seconds=3600)
    assert store.get_unit(item['execution_id'])['dispatch_message'] == 'approved original'
    assert len(core.dispatch_calls) == 1


def test_one_slow_worker_does_not_block_other_worker_and_late_pass_cannot_undo_cancel():
    entered, release, fast_done = Event(), Event(), Event()
    engine = make_engine(RuntimeClass.LOCAL, Clock())
    class Adapter(BoundedTimeoutAdapter):
        def wait(self, run_id, **kwargs):
            if run_id == 'slow':
                entered.set(); release.wait(3)
                return AdapterOutcome(CoreRunStatus.PASS, 'complete')
            return AdapterOutcome(CoreRunStatus.RUNNING, 'pending')
    engine.adapter = Adapter()
    for run in ['slow','fast']:
        engine.dispatch(agent_id='worker',message='test',timeout_seconds=1,core_run_id=run,watchdog_managed=True)
    slow = Thread(target=lambda: engine.wait('slow',timeout_seconds=1))
    fast = Thread(target=lambda: (engine.wait('fast',timeout_seconds=1),fast_done.set()))
    slow.start(); assert entered.wait(1)
    try:
        fast.start(); assert fast_done.wait(0.5), 'unrelated worker wait was serialized'
        engine.mark_user_cancelled('slow')
    finally:
        release.set(); slow.join(3); fast.join(3)
    assert engine.status('slow')['status'] == 'CANCELLED'


def test_transport_poll_timeout_preserves_running_and_does_not_abort():
    engine = make_engine(RuntimeClass.LOCAL, Clock())
    class Adapter(BoundedTimeoutAdapter):
        def wait(self, *args, **kwargs):
            raise TimeoutError('temporary network failure')
    engine.adapter = Adapter()
    engine.dispatch(agent_id='worker',message='test',timeout_seconds=1,core_run_id='network',watchdog_managed=True)
    assert engine.wait('network',timeout_seconds=1)['status'] == 'RUNNING'
    assert engine.adapter.cancelled == []


def test_slow_jev_is_not_a_gate_for_dispatch_or_status():
    entered, release = Event(), Event()
    class Core:
        def dispatch(self, **kwargs): return {'core_run_id':'jev-test','status':'RUNNING'}
        def wait(self, *args, **kwargs): return {'core_run_id':'jev-test','status':'RUNNING'}
        adapter = None
    class Watchdog:
        calls=0
        def observe(self, run_id):
            self.calls += 1; entered.set(); release.wait(3)
    watchdog = Watchdog(); harness = service.PersistentExecutionHarness(Core())
    harness.set_session_watchdog(watchdog)
    started=time.monotonic()
    try:
        assert harness.dispatch(timeout_seconds=1)['status'] == 'RUNNING'
        assert time.monotonic()-started < 0.5
        assert entered.wait(1)
        assert harness.active_controllers
        assert watchdog.calls == 1
    finally:
        release.set(); harness.close()


class RepairableReviewer:
    def __init__(self, root, failure):
        self.root, self.failure = root, failure
        self.provisions, self.reviews = 0, 0
    def create_disposable_session(self, *, agent_id, project_id):
        self.provisions += 1
        if self.failure == 'provision' and self.provisions == 1:
            raise RuntimeError('simulated QA infrastructure failure')
        return {'session_key':f'agent:{agent_id.lower()}:war-room-test:review-{self.provisions}',
                'session_id':f'qa-{self.provisions}', 'purpose':'test','disposable':True}
    def deliver(self, *, delivery_id, agent_id, instruction_id, body):
        self.reviews += 1
        if self.failure == 'format' and self.reviews == 1:
            return DeliveryReceipt(delivery_id,'responded',run_id='qa-bad',response_body='invalid JSON')
        packet=json.loads(body.split('[IMMUTABLE_GROUNDING_PACKET]\n',1)[1].split('\n[ORIGINAL_INSTRUCTION_CONTEXT]',1)[0])
        receipts=[(json.loads(path.read_text()),path) for path in (self.root/'run-receipts').glob('*.json')]
        receipt,path=max(receipts,key=lambda item:item[0]['qa_cycle'])
        result=json.loads(receipt['responses'][0]['body'])
        assert result['summary']=='SIMPLE_UI_TEST_PASS'
        payload={'confirmed_worktree':packet['worktree'],'confirmed_revision':packet['revision'],
                 'verdict':'PASS','summary':'Independent marker check','evidence':[str(path)],
                 'representative_completion_claimed':False}
        return DeliveryReceipt(delivery_id,'responded',run_id=f'qa-{self.reviews}',response_body=json.dumps(payload))
    def close(self): pass


@pytest.mark.parametrize('failure',['provision','format'])
def test_qa_only_resume_preserves_worker_result_revision_and_dispatch_count(production,monkeypatch,failure):
    root,client,_,_=production
    monkeypatch.setenv('PLACHEM_WAR_ROOM_AUTO_QA','1')
    monkeypatch.setenv('PLACHEM_WAR_ROOM_QA_SIGNING_SECRET','isolated-test-secret')
    rpc=WorkerRPC('SIMPLE_UI_TEST_PASS')
    def factory(*args,**kwargs):
        adapter=OpenClawAdapter(*args,**kwargs);adapter.rpc=rpc;return adapter
    monkeypatch.setattr(service,'OpenClawAdapter',factory)
    item,_=prepare_profiled(client,root)
    reviewer=RepairableReviewer(root,failure)
    runtime=WarRoomRuntime(adapter=reviewer)
    database=root/'war-room.sqlite3'
    def state():
        with sqlite3.connect(database) as con:
            con.row_factory=sqlite3.Row
            return dict(con.execute('SELECT * FROM war_tasks WHERE id=?',(item['task_id'],)).fetchone()),con.execute("SELECT COUNT(*) FROM war_processing_issues WHERE task_id=? AND state='OPEN'",(item['task_id'],)).fetchone()[0]
    try:
        for _ in range(100):
            runtime.tick(db_path=database);task,count=state()
            if count: break
            time.sleep(.02)
        assert count and task['status']=='qa' and task['revision']==1
        assert len(rpc.submitted)==1
        rep={'X-Authenticated-Principal':'human-representative','X-War-Room-Proxy-Secret':'fixture-proxy-secret','Idempotency-Key':'qa-only-repair'}
        context=client.get('/api/war-room/projects/plachem-agent-war-room/mutation-context',headers=rep,params={'action':'task_resume_qa','target_id':item['task_id']})
        assert context.status_code==200,context.text
        body={'context_token':context.json()['context_token'],'contract_version':1,'project_id':'plachem-agent-war-room','task_id':item['task_id'],'task_revision':1}
        response=client.post(f"/api/war-room/tasks/{item['task_id']}/resume-qa",headers=rep,json=body)
        assert response.status_code==200,response.text
        assert response.json()['worker_redispatched'] is False
        replay=client.post(f"/api/war-room/tasks/{item['task_id']}/resume-qa",headers=rep,json=body)
        assert replay.status_code==200 and replay.json()==response.json()
        for _ in range(100):
            runtime.tick(db_path=database)
            with sqlite3.connect(database) as con:
                verdict=con.execute('SELECT verdict FROM war_qa_verdicts WHERE task_id=?',(item['task_id'],)).fetchone()
            if verdict: break
            time.sleep(.02)
        assert verdict==('PASS',)
        task,count=state()
        assert task['status']=='qa' and task['revision']==1 and count==0
        assert len(rpc.submitted)==1
        with sqlite3.connect(database) as con:
            con.row_factory=sqlite3.Row
            row=con.execute('SELECT * FROM war_tasks WHERE id=?',(item['task_id'],)).fetchone()
            assert actions._representative_completion_checks(con,row)['missing']==[]
            if failure=='format':
                assert con.execute("SELECT COUNT(*) FROM war_stage_attempts WHERE stage='QA_RETRY'").fetchone()[0]==1
    finally:
        runtime.close()


def test_late_failure_cannot_change_stopped_task(production):
    root,client,_,_=production;item,approval=prepare_profiled(client,root)
    with sqlite3.connect(root/'war-room.sqlite3') as con:
        con.row_factory=sqlite3.Row
        con.execute("UPDATE war_tasks SET status='stopped' WHERE id=?",(item['task_id'],))
        _terminal_validation_failure(con,message_id=item['message_id'],project_id='plachem-agent-war-room',delivery_id=approval['deliveries'][0]['delivery_id'],task_revision=1,error_code='OPENCLAW_ERROR',now=int(time.time()))
        assert con.execute('SELECT status,revision FROM war_tasks WHERE id=?',(item['task_id'],)).fetchone()[:]==('stopped',1)


def test_actual_qa_lane_is_used_for_targeted_stop(production):
    root,client,_,_=production;item,approval=prepare_profiled(client,root)
    class Owner:
        def __init__(self): self.stops=[]
        def stop(self,*,delivery_id,agent_id):
            self.stops.append(agent_id);return DeliveryReceipt(delivery_id,'stopped')
    direct,fast=Owner(),Owner()
    with sqlite3.connect(root/'war-room.sqlite3') as con:
        con.row_factory=sqlite3.Row
        row=dict(con.execute('SELECT * FROM war_deliveries WHERE id=?',(approval['deliveries'][0]['delivery_id'],)).fetchone())
        _,result=stop_bound_delivery(con,row,adapter=direct,adapter_selector=lambda mode,path:fast)
        assert result.status=='stopped'
        row.update(agent_id='ERPqa',id='qa-row')
        _,result=stop_bound_delivery(con,row,adapter=direct,adapter_selector=lambda mode,path:fast)
        assert result.status=='stopped'
    assert fast.stops==['ERPmanager'] and direct.stops==['ERPqa']


def test_expired_queue_is_not_kept_busy_forever(production):
    root,client,_,_=production;item,approval=prepare_profiled(client,root);now=int(time.time())
    database=root/'war-room.sqlite3';delivery=approval['deliveries'][0]['delivery_id']
    with sqlite3.connect(database) as con:
        con.execute('UPDATE war_deliveries SET deadline_at=? WHERE id=?',(now-1,delivery))
        con.execute("INSERT INTO war_deliveries(id,message_id,agent_id,task_revision,status,created_at) VALUES ('busy-other',?,'ERPmanager',2,'received',?)",(item['message_id'],now))
    class NoDispatch:
        def deliver(self,**kwargs): raise AssertionError('expired task must not execute')
    result=process_due_deliveries(db_path=database,adapter=NoDispatch(),now=now)
    assert result[0]['status']=='timed_out'
