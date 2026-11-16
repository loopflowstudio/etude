-- after: run_names
-- Keep the provider's answer on the session, independent of diagnostic retention.
ALTER TABLE sessions ADD COLUMN refusal TEXT;
CREATE INDEX sessions_refused ON sessions(refused_at DESC) WHERE refused_at IS NOT NULL;
CREATE INDEX sessions_finishing ON sessions(seq)
  WHERE finish_requested_at IS NOT NULL AND ended_at IS NULL;
