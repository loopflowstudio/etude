-- after: live_machine_inventory, provider_units
-- breaks: older schedulers admit runs past a stored experiment limit
CREATE TABLE experiment_limits (
    experiment TEXT PRIMARY KEY NOT NULL,
    max_usd TEXT NOT NULL,
    set_at TEXT NOT NULL
);

CREATE INDEX launches_reserving ON launches(id)
    WHERE json_extract(body, '$.state') IN ('preparing', 'admitting');
