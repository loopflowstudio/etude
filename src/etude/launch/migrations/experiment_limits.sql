-- after: live_machine_inventory, provider_units
-- breaks: older schedulers admit runs past a stored experiment limit
CREATE TABLE experiment_limits (
    experiment TEXT PRIMARY KEY NOT NULL,
    max_usd TEXT NOT NULL,
    set_at TEXT NOT NULL
);

CREATE INDEX launches_reserving ON launches(rtrim(json_extract(body, '$.expires_at'), 'Z'))
    WHERE json_extract(body, '$.state') IN ('preparing', 'admitting');

CREATE INDEX launch_acquisitions ON launch_sessions(session, launch) WHERE acquired = 1;
