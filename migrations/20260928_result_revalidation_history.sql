-- Result-only revalidation history.  The original execution row is never rewritten.
CREATE TABLE IF NOT EXISTS war_result_revalidation_history (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES war_tasks(id),
  task_revision INTEGER NOT NULL,
  core_run_id TEXT NOT NULL,
  openclaw_run_id TEXT,
  session_key TEXT,
  validator_version TEXT NOT NULL,
  reason TEXT NOT NULL,
  outcome TEXT NOT NULL,
  original_run_status TEXT NOT NULL,
  original_response TEXT,
  dispatch_count INTEGER NOT NULL,
  requested_at INTEGER NOT NULL,
  UNIQUE(task_id, task_revision, core_run_id, requested_at)
);
CREATE INDEX IF NOT EXISTS idx_war_result_revalidation_task
  ON war_result_revalidation_history(task_id, task_revision, requested_at DESC);
CREATE TRIGGER IF NOT EXISTS war_result_revalidation_no_update
  BEFORE UPDATE ON war_result_revalidation_history
  BEGIN SELECT RAISE(ABORT, 'result revalidation history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS war_result_revalidation_no_delete
  BEFORE DELETE ON war_result_revalidation_history
  BEGIN SELECT RAISE(ABORT, 'result revalidation history is append-only'); END;
