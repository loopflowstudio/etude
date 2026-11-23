-- after: spot_rentals
-- A live listing visits owned machines, open sessions and occupied slots.
CREATE INDEX machines_owned ON machines(name) WHERE pool IS NOT NULL;
CREATE INDEX sessions_open_machines ON sessions(machine) WHERE ended_at IS NULL AND refused_at IS NULL;
CREATE INDEX sessions_machine ON sessions(machine);
CREATE INDEX attempts_allocated ON attempts(machine, run) WHERE outcome IS NULL;
