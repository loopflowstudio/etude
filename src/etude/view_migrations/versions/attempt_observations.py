"""Fence mutable evidence by attempt and retain its latest full observation."""

from alembic import op

revision = "attempt_observations"
down_revision = "training_curves"


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN latest_attempt_number integer NOT NULL DEFAULT 0")
    op.execute("""
        UPDATE runs SET latest_attempt_number = f.number
        FROM (SELECT address, max(attempt_number) AS number FROM run_files GROUP BY address) f
        WHERE f.address = runs.address
    """)
    op.execute("""
        CREATE TABLE attempts (
            address text NOT NULL REFERENCES runs(address) ON DELETE CASCADE,
            number integer NOT NULL,
            attempt_id text NOT NULL UNIQUE,
            producer_seq bigint NOT NULL,
            console_position bigint NOT NULL,
            finished boolean NOT NULL,
            observation text NOT NULL,
            PRIMARY KEY (address, number)
        )
    """)
    op.execute("ALTER TABLE run_charts ADD COLUMN checkpoints text")
