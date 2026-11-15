-- after: run_resume_and_writers
CREATE INDEX runs_name ON runs(json_extract(spec, '$.name'), seq);
