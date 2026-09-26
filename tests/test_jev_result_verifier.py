import json, sqlite3, urllib.request
import jev_result_verifier as verifier

def make_db(tmp_path, monkeypatch):
    path = tmp_path / "war.sqlite3"; monkeypatch.setattr(verifier.war_room, "_db_path", lambda: path)
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE war_tasks(id TEXT PRIMARY KEY, project_id TEXT, scope TEXT, status TEXT, revision INTEGER, assignee_agent_id TEXT, source_message_id TEXT, delivery_state TEXT, approval_state TEXT, execution_state TEXT, qa_state TEXT);
        CREATE TABLE war_grounding_packets(task_id TEXT PRIMARY KEY, packet_json TEXT);
        CREATE TABLE war_execution_runs(core_run_id TEXT PRIMARY KEY, war_task_id TEXT, agent_id TEXT, run_status TEXT, result_json TEXT, artifacts_json TEXT, evidence_json TEXT, policy_status TEXT, validation_error TEXT, updated_at INTEGER);
        CREATE TABLE war_evidence(task_id TEXT, evidence_type TEXT, uri TEXT, summary TEXT, sha256 TEXT, run_id TEXT, created_at INTEGER);
        """)
        con.execute("INSERT INTO war_tasks VALUES ('t1','p1','scope secret=hidden','qa',3,NULL,NULL,'delivered','approved','running','pending')")
        con.execute("INSERT INTO war_grounding_packets VALUES ('t1',?)", (json.dumps({'purpose':'goal','completion_conditions':['test','log']}),))
        con.execute("INSERT INTO war_execution_runs VALUES ('run-1','t1','ERPcoder','completed','{\"typed\":true}','[{\"path\":\"report.txt\"}]','[{\"uri\":\"log.txt\",\"sha256\":\"abc\"}]','PASS',NULL,10)")
    verifier.provision_schema(str(path)); return path

class Fake:
    def __init__(self): self.calls=[]
    def evaluate(self, *, signal, state, idempotency_key):
        self.calls.append((signal, state, idempotency_key)); return {'choice':'true','probabilities':{'true':.8,'false':.1,'uncertain':.1}}

def test_four_signals_parallel_state_and_secret_redaction(tmp_path, monkeypatch):
    path = make_db(tmp_path, monkeypatch); fake=Fake(); verifier.set_result_verifier_client(fake)
    result=verifier.create_result_verifier_advisory('t1')
    assert {x['signal'] for x in result['signals']} == set(verifier.SIGNALS); assert len(fake.calls)==4
    assert all(call[1]['revision']==3 and call[1]['worker']['typed_results'] for call in fake.calls)
    assert 'hidden' not in json.dumps(result)
    with sqlite3.connect(path) as con:
        assert con.execute('select count(*) from jev_result_verifier_advisories').fetchone()[0] == 4
        stored = con.execute('select state_json from jev_result_verifier_advisories limit 1').fetchone()[0]
        assert 'hidden' not in stored and '***REDACTED***' in stored

def test_unavailable_fallback_stale_and_append_only(tmp_path, monkeypatch):
    path=make_db(tmp_path, monkeypatch); verifier.set_result_verifier_client(verifier.UnavailableResultVerifierClient())
    result=verifier.create_result_verifier_advisory('t1'); assert all(x['status']=='ADVISORY_UNAVAILABLE' for x in result['signals'])
    with sqlite3.connect(path) as con:
        con.execute("update war_tasks set revision=4 where id='t1'"); con.commit()
        assert verifier.get_result_verifier_advisory('t1')['stale'] is True
        try: con.execute("delete from jev_result_verifier_advisories")
        except sqlite3.IntegrityError as exc: assert 'append-only' in str(exc)
        else: raise AssertionError('append-only trigger missing')

def test_create_requires_latest_worker_terminal_result(tmp_path, monkeypatch):
    path=make_db(tmp_path, monkeypatch)
    with sqlite3.connect(path) as con:
        con.execute("UPDATE war_execution_runs SET run_status='responded' WHERE core_run_id='run-1'")
        con.commit()
    try:
        verifier.create_result_verifier_advisory('t1')
    except Exception as exc:
        assert getattr(exc, 'status_code', None) == 409
    else:
        raise AssertionError('non-terminal Worker result must be rejected')

def test_noul_uses_one_openconnector_request_and_boolean_answers(tmp_path, monkeypatch):
    token = tmp_path / 'jev-token'; token.write_text('fixture', encoding='utf-8')
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self):
            return json.dumps({'success': True, 'data': {'answers': {
                signal: {'type': 'boolean', 'probability': 0.8 if signal == 'requirements_met' else 0.2}
                for signal in verifier.SIGNALS
            }}}).encode()
    def fake_urlopen(request, timeout):
        captured['url'] = request.full_url; captured['headers'] = dict(request.header_items()); captured['body'] = json.loads(request.data); return Response()
    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    client = verifier.NoulResultVerifierClient(endpoint='https://jev.example/evaluate', token_file=str(token))
    result = client.evaluate_signals(signals=verifier.SIGNALS, state={'revision': 3}, idempotency_key='one-request')
    assert captured['url'] == 'https://jev.example/evaluate'
    assert captured['headers']['Authorization'] == 'Bearer fixture'
    assert captured['headers']['Idempotency-key'] == 'one-request'
    assert captured['body']['connectionName'] == 'default'
    assert set(captured['body']['input']['questions']) == set(verifier.SIGNALS)
    assert all(question['type'] == 'boolean' for question in captured['body']['input']['questions'].values())
    assert result['requirements_met']['answer'] is True
    assert result['requirements_met']['probability'] == 0.8

def test_advisory_does_not_mutate_lifecycle_or_assignment(tmp_path, monkeypatch):
    path=make_db(tmp_path, monkeypatch); verifier.set_result_verifier_client(Fake())
    before=sqlite3.connect(path).execute('select status,revision,assignee_agent_id,delivery_state,approval_state,execution_state,qa_state from war_tasks').fetchone(); verifier.create_result_verifier_advisory('t1'); after=sqlite3.connect(path).execute('select status,revision,assignee_agent_id,delivery_state,approval_state,execution_state,qa_state from war_tasks').fetchone(); assert before==after

def test_ui_and_api_contract_strings():
    root=verifier.__file__.rsplit('/',1)[0]
    html=open(root+'/static/war-room.html',encoding='utf-8').read(); js=open(root+'/static/war-room-ui.js',encoding='utf-8').read()
    assert 'JEV Result Verifier · advisory only' in html and '/jev-result-verifier' in js
