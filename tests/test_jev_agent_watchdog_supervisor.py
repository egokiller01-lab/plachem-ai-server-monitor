import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jev_agent_watchdog_supervisor as sup


def _row(start_s, last_s=None, status="running"):
    return {
        "agentId": "erpmanager",
        "sessionId": "session-1",
        "key": "agent:erpmanager:test",
        "status": status,
        "sessionStartedAt": int(start_s * 1000) if start_s is not None else None,
        "lastInteractionAt": int((last_s if last_s is not None else start_s) * 1000),
    }


def _wire(monkeypatch, row, calls):
    monkeypatch.setattr(sup, "sessions", lambda: [row])
    monkeypatch.setattr(sup, "_session_context", lambda agent_id, sid: {})
    monkeypatch.setattr(sup, "_source_task_active", lambda ctx: (True, "active_task_run"))
    monkeypatch.setattr(
        sup,
        "post",
        lambda s: calls.append(s.copy()) or {"choice": "CONTINUE", "action": None},
    )
    sup._last.clear()
    sup._first_seen.clear()


def test_grace_is_from_session_start_not_last_interaction(monkeypatch):
    start = 1_000.0
    row = _row(start, last_s=start + 299)
    calls = []
    _wire(monkeypatch, row, calls)

    out = sup.tick(now=start + 301)

    assert len(calls) == 1
    assert out[0]["choice"] == "CONTINUE"
    assert out[0]["age"] == 301.0


def test_recent_interaction_does_not_reset_post_grace_cadence(monkeypatch):
    start = 2_000.0
    row = _row(start, last_s=start + 290)
    calls = []
    _wire(monkeypatch, row, calls)

    assert len(sup.tick(now=start + 301)) == 1
    row["lastInteractionAt"] = int((start + 320) * 1000)
    assert sup.tick(now=start + 330) == []
    assert len(sup.tick(now=start + 362)) == 1
    assert len(calls) == 2


def test_before_five_minutes_is_not_polled(monkeypatch):
    start = 3_000.0
    row = _row(start, last_s=start + 299)
    calls = []
    _wire(monkeypatch, row, calls)

    assert sup.tick(now=start + 299.9) == []
    assert calls == []


def test_missing_session_start_uses_stable_first_seen_anchor(monkeypatch):
    base = 4_000.0
    row = _row(None, last_s=base)
    calls = []
    _wire(monkeypatch, row, calls)

    assert sup.tick(now=base + 10) == []
    row["lastInteractionAt"] = int((base + 250) * 1000)
    assert sup.tick(now=base + 309) == []
    assert len(sup.tick(now=base + 311)) == 1
    assert len(calls) == 1


def test_abnormal_token_evidence_is_exact_fresh_session_only(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "telemetry.sqlite3"
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE token_telemetry_samples (
              sample_id INTEGER PRIMARY KEY, sampled_at_epoch INTEGER, agent TEXT,
              session_id TEXT, classification TEXT, current_prompt_tokens INTEGER,
              context_limit INTEGER, context_pressure REAL, delta_processed INTEGER,
              velocity_tokens_per_hour REAL, calls_per_hour REAL,
              delta_cache_ratio REAL, reasons_json TEXT
            )
        """)
        con.execute(
            "INSERT INTO token_telemetry_samples VALUES (1,950,'erpmanager','s1','ABNORMAL',185000,200000,0.925,1010000,12120000,12,0.95,?)",
            (json.dumps(["CACHE_CHURN"]),),
        )
        con.execute(
            "INSERT INTO token_telemetry_samples VALUES (2,950,'erpmanager','s2','HIGH',150000,200000,0.75,1000,1000,1,0.5,'[]')"
        )
        con.commit()
        con.close()
        monkeypatch.setattr(sup, "TOKEN_TELEMETRY_DB", db)
        value = sup._abnormal_token_evidence("erpmanager", "s1", now=1000)
        assert value["classification"] == "ABNORMAL"
        assert value["current_prompt_tokens"] == 185000
        assert value["reasons"] == ["CACHE_CHURN"]
        assert sup._abnormal_token_evidence("erpmanager", "s2", now=1000) is None
        assert sup._abnormal_token_evidence("erpmanager", "s1", now=2000) is None


def test_post_adds_only_returned_abnormal_token_evidence(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{}'
    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode())
        return Response()

    monkeypatch.setattr(sup.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        sup, "_abnormal_token_evidence",
        lambda agent, sid: {"classification":"ABNORMAL","reasons":["CACHE_CHURN"]},
    )
    sup.post(_row(1))
    assert captured["body"]["evidence"]["token_telemetry"]["classification"] == "ABNORMAL"

    monkeypatch.setattr(sup, "_abnormal_token_evidence", lambda agent, sid: None)
    sup.post(_row(1))
    assert "token_telemetry" not in captured["body"]["evidence"]


def test_token_telemetry_is_volatile_for_stable_digest():
    import jev_agent_recovery_watchdog_v01 as base
    left = {"progress":{"detection_evidence":{"token_telemetry":{"delta_processed":1}}}, "status":"RUNNING"}
    right = {"progress":{"detection_evidence":{"token_telemetry":{"delta_processed":999999}}}, "status":"RUNNING"}
    assert base._stable_state_material(left) == base._stable_state_material(right)


def test_post_error_retries_after_short_delay_not_full_cadence(monkeypatch):
    start = 5_000.0
    row = _row(start, last_s=start + 300)
    calls = []
    monkeypatch.setattr(sup, "sessions", lambda: [row])
    monkeypatch.setattr(sup, "_session_context", lambda agent_id, sid: {})
    monkeypatch.setattr(sup, "_source_task_active", lambda ctx: (True, "active_task_run"))
    sup._last.clear()
    sup._first_seen.clear()

    def flaky_post(value):
        calls.append(value.copy())
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"choice": "CONTINUE", "action": "OBSERVE_ONLY"}

    monkeypatch.setattr(sup, "post", flaky_post)

    first = sup.tick(now=start + 301)
    assert len(calls) == 1
    assert first[0]["error"] == "RuntimeError"
    assert first[0]["retry_after_seconds"] == sup.ERROR_RETRY_SECONDS

    # No immediate duplicate decision after an uncertain response.
    assert sup.tick(now=start + 310) == []
    assert len(calls) == 1

    retried = sup.tick(now=start + 316)
    assert len(calls) == 2
    assert retried[0]["choice"] == "CONTINUE"
