-- after: run_resume_and_writers
-- breaks: etude 0.4.0 inserts runs.cohort
ALTER TABLE runs DROP COLUMN cohort;
DROP TABLE cohorts;
CREATE INDEX runs_name ON runs(json_extract(spec, '$.name'), seq);
