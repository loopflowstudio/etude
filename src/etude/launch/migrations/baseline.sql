-- The schema released in etude 0.3.1 and 0.3.2.
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS transfers_delivery ON transfers(
  json_extract(body, '$.attempt'), julianday(json_extract(body, '$.observed_at')) DESC)
  WHERE json_extract(body, '$.completed') = 1
    AND json_extract(body, '$.status') BETWEEN 200 AND 299
    AND json_extract(body, '$.operation') IN ('PUT', 'POST');
CREATE TABLE IF NOT EXISTS launches(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS launch_runs(launch TEXT NOT NULL, run TEXT NOT NULL,
  PRIMARY KEY(launch, run));
CREATE INDEX IF NOT EXISTS launch_runs_run ON launch_runs(run);
CREATE TABLE IF NOT EXISTS launch_sessions(launch TEXT NOT NULL, session TEXT NOT NULL, acquired INTEGER NOT NULL,
  PRIMARY KEY(launch, session));
CREATE TABLE IF NOT EXISTS diagnostics(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log_streams(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks(
  supply TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings(name, value) VALUES ('default_pool', 'local');
INSERT OR IGNORE INTO settings(name, value) VALUES ('default_rent', '');
CREATE TABLE IF NOT EXISTS cohorts(
  name TEXT PRIMARY KEY, parallel INTEGER NOT NULL, max_usd TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS providers(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, registry TEXT NOT NULL, storage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS storages(
  name TEXT PRIMARY KEY, bucket TEXT NOT NULL, prefix TEXT NOT NULL, provider TEXT NOT NULL,
  region TEXT);
CREATE TABLE IF NOT EXISTS pools(
  name TEXT PRIMARY KEY, off INTEGER NOT NULL DEFAULT 0,
  startup_seconds REAL, delivery_seconds REAL, log_seconds REAL,
  slots INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS ondemands(name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machines(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL,
  pool TEXT REFERENCES pools(name), ondemand TEXT REFERENCES ondemands(name),
  cost_usd TEXT NOT NULL DEFAULT '0', expires_at TEXT,
  provider TEXT, address TEXT, storage TEXT, deleted_at TEXT, image TEXT,
  CHECK ((pool IS NULL) != (ondemand IS NULL)));
CREATE TABLE IF NOT EXISTS runs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  job TEXT NOT NULL, spec TEXT NOT NULL, seed INTEGER,
  pool TEXT REFERENCES pools(name), rent TEXT REFERENCES ondemands(name),
  cohort TEXT REFERENCES cohorts(name),
  created_at TEXT NOT NULL,
  cancel_requested_at TEXT, version INTEGER NOT NULL DEFAULT 1,
  autorun INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '{}', source TEXT, smoke TEXT NOT NULL DEFAULT '{}',
  CHECK (pool IS NOT NULL OR rent IS NOT NULL));
CREATE TABLE IF NOT EXISTS sessions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  machine TEXT NOT NULL REFERENCES machines(name), planned_at TEXT NOT NULL,
  claimed_at TEXT, startup_deadline TEXT, provider_id TEXT, started_at TEXT, refused_at TEXT, ended_at TEXT,
  version INTEGER NOT NULL DEFAULT 1, autorun INTEGER NOT NULL DEFAULT 0, hardware TEXT,
  finish_requested_at TEXT);
CREATE TABLE IF NOT EXISTS rentals(
  machine TEXT PRIMARY KEY REFERENCES machines(name), usd_per_hour TEXT NOT NULL,
  cap_usd TEXT NOT NULL, deadline TEXT NOT NULL, kept INTEGER NOT NULL DEFAULT 0,
  billed_usd TEXT NOT NULL DEFAULT '0', billed_through TEXT);
CREATE TABLE IF NOT EXISTS attempts(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  run TEXT NOT NULL REFERENCES runs(id), session TEXT NOT NULL REFERENCES sessions(id), machine TEXT NOT NULL REFERENCES machines(name),
  number INTEGER NOT NULL, position INTEGER NOT NULL, limit_seconds INTEGER NOT NULL,
  limit_kind TEXT NOT NULL, started_at TEXT, finished_at TEXT, outcome TEXT, cause TEXT,
  clocks TEXT NOT NULL DEFAULT '{}', outputs TEXT NOT NULL DEFAULT '[]', exit_code INTEGER,
  version INTEGER NOT NULL DEFAULT 1, planned_at TEXT, added INTEGER NOT NULL DEFAULT 0,
  prepared TEXT, tally TEXT);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job);
CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run);
CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session);
