-- after: baseline
ALTER TABLE runs ADD COLUMN resume_after INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attempts ADD COLUMN writers TEXT NOT NULL DEFAULT '[]';
DROP INDEX attempts_run;
CREATE INDEX attempts_run ON attempts(run, number);
CREATE INDEX runs_experiment ON runs(json_extract(spec, '$.experiment'), seq);
CREATE INDEX runs_pool ON runs(pool, seq);
CREATE INDEX runs_rent ON runs(rent, seq);
