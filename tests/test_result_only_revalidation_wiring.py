import ast
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_result_only_endpoint_is_authorized_idempotent_and_never_dispatches():
    source = (ROOT / "war_room_actions.py").read_text()
    tree = ast.parse(source)
    endpoint = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "revalidate_task_result")
    body = ast.get_source_segment(source, endpoint)
    assert "_require_representative" in body
    assert "_validate_mutation_contract" in body
    assert "_require_fresh_context" in body
    assert "_idem" in body and "_save_idem" in body
    assert "war_result_revalidation_history" in body
    assert "war_execution_runs SET" not in body
    assert "war_task_calls SET" not in body
    assert "dispatch_execution" not in body


def test_result_revalidation_history_is_append_only():
    migration = (ROOT / "migrations/20260928_result_revalidation_history.sql").read_text()
    assert "validator_version" in migration
    assert "original_response" in migration
    assert "dispatch_count" in migration
    with sqlite3.connect(":memory:") as con:
        con.execute("CREATE TABLE war_tasks(id TEXT PRIMARY KEY)")
        con.executescript(migration)
        con.execute("INSERT INTO war_tasks VALUES ('task-1')")
        con.execute("""INSERT INTO war_result_revalidation_history
            VALUES ('h','task-1',1,'core','openclaw','session','v1','reason','FAIL','FAIL','original',1,1)""")
        try:
            con.execute("UPDATE war_result_revalidation_history SET reason='tampered'")
            raise AssertionError("history update was allowed")
        except sqlite3.DatabaseError as exc:
            assert "append-only" in str(exc)


def test_simple_ui_wires_eligible_result_only_button():
    source = (ROOT / "static/war-room-simple.js").read_text()
    assert "결과만 재검증" in source
    assert "revalidate-result" in source
    assert "task_revalidate_result" in source
    assert "task.revalidation?.eligible" in source
    assert "worker_redispatched: False" not in source
