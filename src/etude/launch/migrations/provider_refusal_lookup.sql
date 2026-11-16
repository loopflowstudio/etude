-- after: run_names
-- Bound the queued-create refusal pause by time, including after a launcher restart.
CREATE INDEX sessions_refused ON sessions(refused_at DESC) WHERE refused_at IS NOT NULL;
CREATE INDEX diagnostics_session_operation ON diagnostics(
  CAST(json_extract(body, '$.session') AS TEXT), json_extract(body, '$.operation'));
CREATE INDEX sessions_finishing ON sessions(seq)
  WHERE finish_requested_at IS NOT NULL AND ended_at IS NULL;
