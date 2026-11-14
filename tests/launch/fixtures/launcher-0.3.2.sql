-- Written by released Database and Scheduler code at v0.3.2.
BEGIN TRANSACTION;
CREATE TABLE attempts(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  run TEXT NOT NULL REFERENCES runs(id), session TEXT NOT NULL REFERENCES sessions(id), machine TEXT NOT NULL REFERENCES machines(name),
  number INTEGER NOT NULL, position INTEGER NOT NULL, limit_seconds INTEGER NOT NULL,
  limit_kind TEXT NOT NULL, started_at TEXT, finished_at TEXT, outcome TEXT, cause TEXT,
  clocks TEXT NOT NULL DEFAULT '{}', outputs TEXT NOT NULL DEFAULT '[]', exit_code INTEGER,
  version INTEGER NOT NULL DEFAULT 1, planned_at TEXT, added INTEGER NOT NULL DEFAULT 0,
  prepared TEXT, tally TEXT);
INSERT INTO "attempts" VALUES(1,'39ff259e541a','1536332b177e','5094ca2eada9','bench',1,0,3600,'out_of_time','2026-11-14T00:00:00+00:00',NULL,NULL,NULL,'{}','[]',NULL,2,'2026-11-14T00:00:00+00:00',0,NULL,NULL);
CREATE TABLE cohorts(
  name TEXT PRIMARY KEY, parallel INTEGER NOT NULL, max_usd TEXT,
  created_at TEXT NOT NULL);
INSERT INTO "cohorts" VALUES('sweep',2,NULL,'2026-11-14T00:00:00+00:00');
CREATE TABLE diagnostics(id TEXT PRIMARY KEY, body TEXT NOT NULL);
INSERT INTO "diagnostics" VALUES('0514eac479cec751737a3bcf45774d2fda282620be464bb3ba2d494dfdc98e05','{"id":"0514eac479cec751737a3bcf45774d2fda282620be464bb3ba2d494dfdc98e05","observed_at":"2026-11-14T00:00:00Z","produced_at":null,"operation":"preparation","message":"awaiting prepared source; job command not started","launch":"6109295aed84","session":null,"attempt":null,"exception":null,"exit_code":null,"timeout":null}');
INSERT INTO "diagnostics" VALUES('d373757af95b4d0e11b98109ad05b0b5737bd4a216c291cc6683dcda2faf011f','{"id":"d373757af95b4d0e11b98109ad05b0b5737bd4a216c291cc6683dcda2faf011f","observed_at":"2026-11-14T00:00:00Z","produced_at":null,"operation":"source preparation","message":"preparing requested work; job command not started","launch":"6109295aed84","session":null,"attempt":null,"exception":null,"exit_code":null,"timeout":null}');
INSERT INTO "diagnostics" VALUES('49082c016c06ab2d5736e994c188fedc87216ee87a8a824e53c214e8190b5903','{"id":"49082c016c06ab2d5736e994c188fedc87216ee87a8a824e53c214e8190b5903","observed_at":"2026-11-14T00:00:00Z","produced_at":null,"operation":"admission","message":"run admitted","launch":"6109295aed84","session":null,"attempt":null,"exception":null,"exit_code":null,"timeout":null}');
INSERT INTO "diagnostics" VALUES('c30766cd11fa4528cfd57a0443c0f9cfb3c76ed1136bc750e6e59625effaa0fd','{"id":"c30766cd11fa4528cfd57a0443c0f9cfb3c76ed1136bc750e6e59625effaa0fd","observed_at":"2026-11-14T00:00:00Z","produced_at":null,"operation":"preparation","message":"awaiting prepared source; job command not started","launch":"8f65b608f558","session":null,"attempt":null,"exception":null,"exit_code":null,"timeout":null}');
INSERT INTO "diagnostics" VALUES('fa26402bb547edc6387d714909c288ded91199309f8455f400d5cfd9b8f08ad5','{"id":"fa26402bb547edc6387d714909c288ded91199309f8455f400d5cfd9b8f08ad5","observed_at":"2026-11-14T00:00:00Z","produced_at":null,"operation":"source preparation","message":"preparing requested work; job command not started","launch":"8f65b608f558","session":null,"attempt":null,"exception":null,"exit_code":null,"timeout":null}');
INSERT INTO "diagnostics" VALUES('71fe24ede83724e30d1c3a4eaea9210bc1df1308b1eb6bcbe4c87e701e58dde8','{"id":"71fe24ede83724e30d1c3a4eaea9210bc1df1308b1eb6bcbe4c87e701e58dde8","observed_at":"2026-11-14T00:00:00Z","produced_at":null,"operation":"admission","message":"run admitted","launch":"8f65b608f558","session":null,"attempt":null,"exception":null,"exit_code":null,"timeout":null}');
CREATE TABLE launch_runs(launch TEXT NOT NULL, run TEXT NOT NULL,
  PRIMARY KEY(launch, run));
INSERT INTO "launch_runs" VALUES('6109295aed84','1536332b177e');
INSERT INTO "launch_runs" VALUES('8f65b608f558','f7d752e612b4');
CREATE TABLE launch_sessions(launch TEXT NOT NULL, session TEXT NOT NULL, acquired INTEGER NOT NULL,
  PRIMARY KEY(launch, session));
CREATE TABLE launches(id TEXT PRIMARY KEY, body TEXT NOT NULL);
INSERT INTO "launches" VALUES('6109295aed84','{"id":"6109295aed84","requests":[{"run":"1536332b177e","fingerprint":"3f2e062259778410fd2ac70f7911a84284a1c21382eba6643d621f8bd6545adb","requested_revision":null,"resolved_revision":null}],"created_at":"2026-11-14T00:00:00Z","state":"admitted"}');
INSERT INTO "launches" VALUES('8f65b608f558','{"id":"8f65b608f558","requests":[{"run":"f7d752e612b4","fingerprint":"24c3da7f734293f87b2a7eba518c51fcce3783fcc38f1a8545f956446c866b14","requested_revision":null,"resolved_revision":null}],"created_at":"2026-11-14T00:00:00Z","state":"admitted"}');
CREATE TABLE locks(
  supply TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE log_streams(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE machines(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL,
  pool TEXT REFERENCES pools(name), ondemand TEXT REFERENCES ondemands(name),
  cost_usd TEXT NOT NULL DEFAULT '0', expires_at TEXT,
  provider TEXT, address TEXT, storage TEXT, deleted_at TEXT, image TEXT,
  CHECK ((pool IS NULL) != (ondemand IS NULL)));
INSERT INTO "machines" VALUES('bench','local','bench',NULL,'0',NULL,NULL,NULL,NULL,NULL,NULL);
INSERT INTO "machines" VALUES('paid-machine','rented',NULL,'gpu','0',NULL,'account',NULL,NULL,NULL,NULL);
CREATE TABLE ondemands(name TEXT PRIMARY KEY, body TEXT NOT NULL);
INSERT INTO "ondemands" VALUES('gpu','{"name":"gpu","template":{"provider":"account","gpu":"NVIDIA L4","gpu_count":1,"vcpus":6,"memory_gb":48,"disk_gb":null,"image":{"requested":"fixture","resolved":"fixture@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","platform":null},"data_center":null},"max_machines":1,"max_usd_per_hour":null,"slots":1,"startup_seconds":null,"delivery_seconds":null,"log_seconds":null,"setup_seconds":900,"storage":null}');
CREATE TABLE pools(
  name TEXT PRIMARY KEY, off INTEGER NOT NULL DEFAULT 0,
  startup_seconds REAL, delivery_seconds REAL, log_seconds REAL,
  slots INTEGER NOT NULL DEFAULT 1);
INSERT INTO "pools" VALUES('bench',0,NULL,NULL,NULL,1);
CREATE TABLE providers(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, registry TEXT NOT NULL, storage TEXT NOT NULL);
INSERT INTO "providers" VALUES('account','runpod','','bucket');
CREATE TABLE rentals(
  machine TEXT PRIMARY KEY REFERENCES machines(name), usd_per_hour TEXT NOT NULL,
  cap_usd TEXT NOT NULL, deadline TEXT NOT NULL, kept INTEGER NOT NULL DEFAULT 0,
  billed_usd TEXT NOT NULL DEFAULT '0', billed_through TEXT);
INSERT INTO "rentals" VALUES('paid-machine','0.59','1','2026-11-14T01:00:00+00:00',0,'0',NULL);
CREATE TABLE runs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  job TEXT NOT NULL, spec TEXT NOT NULL, seed INTEGER,
  pool TEXT REFERENCES pools(name), rent TEXT REFERENCES ondemands(name),
  cohort TEXT REFERENCES cohorts(name),
  created_at TEXT NOT NULL,
  cancel_requested_at TEXT, version INTEGER NOT NULL DEFAULT 1,
  autorun INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '{}', source TEXT, smoke TEXT NOT NULL DEFAULT '{}',
  CHECK (pool IS NOT NULL OR rent IS NOT NULL));
INSERT INTO "runs" VALUES(1,'1536332b177e','26cccefdf941','{"setup":null,"cleanup":null,"stop_seconds":null,"cleanup_seconds":null,"runtime":null,"prepare":null,"command":"train","max_hours":1.0,"max_usd":null,"name":"","until":null,"config":null,"repo":null,"revision":null,"max_attempts":3,"inputs":[],"environment":[],"experiment":null,"smoke":null}',NULL,'bench',NULL,'sweep','2026-11-14T00:00:00+00:00',NULL,1,1,0,'{}',NULL,'{"steps":[]}');
INSERT INTO "runs" VALUES(2,'f7d752e612b4','6d2a8a2d68c4','{"setup":null,"cleanup":null,"stop_seconds":null,"cleanup_seconds":null,"runtime":null,"prepare":null,"command":"pending","max_hours":1.0,"max_usd":null,"name":"","until":null,"config":null,"repo":null,"revision":null,"max_attempts":3,"inputs":[],"environment":[],"experiment":null,"smoke":null}',NULL,'bench',NULL,NULL,'2026-11-14T00:00:00+00:00',NULL,1,1,0,'{}',NULL,'{"steps":[]}');
CREATE TABLE sessions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  machine TEXT NOT NULL REFERENCES machines(name), planned_at TEXT NOT NULL,
  claimed_at TEXT, startup_deadline TEXT, provider_id TEXT, started_at TEXT, refused_at TEXT, ended_at TEXT,
  version INTEGER NOT NULL DEFAULT 1, autorun INTEGER NOT NULL DEFAULT 0, hardware TEXT,
  finish_requested_at TEXT);
INSERT INTO "sessions" VALUES(1,'5094ca2eada9','bench','2026-11-14T00:00:00+00:00','2026-11-14T00:00:00+00:00',NULL,'5094ca2eada9','2026-11-14T00:00:00+00:00',NULL,NULL,4,0,NULL,NULL);
INSERT INTO "sessions" VALUES(2,'paid-session','paid-machine','2026-11-14T00:00:00+00:00',NULL,NULL,NULL,NULL,NULL,NULL,1,0,NULL,NULL);
CREATE TABLE settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "settings" VALUES('default_pool','local');
INSERT INTO "settings" VALUES('default_rent','');
CREATE TABLE storages(
  name TEXT PRIMARY KEY, bucket TEXT NOT NULL, prefix TEXT NOT NULL, provider TEXT NOT NULL,
  region TEXT);
INSERT INTO "storages" VALUES('bucket','fixture','runs','s3','us-west-2');
CREATE TABLE transfers(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE INDEX transfers_delivery ON transfers(
  json_extract(body, '$.attempt'), julianday(json_extract(body, '$.observed_at')) DESC)
  WHERE json_extract(body, '$.completed') = 1
    AND json_extract(body, '$.status') BETWEEN 200 AND 299
    AND json_extract(body, '$.operation') IN ('PUT', 'POST');
CREATE INDEX launch_runs_run ON launch_runs(run);
CREATE INDEX runs_job ON runs(job);
CREATE INDEX attempts_run ON attempts(run);
CREATE INDEX attempts_session ON attempts(session);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('runs',2);
INSERT INTO "sqlite_sequence" VALUES('sessions',2);
INSERT INTO "sqlite_sequence" VALUES('attempts',1);
COMMIT;
PRAGMA user_version = 17;
