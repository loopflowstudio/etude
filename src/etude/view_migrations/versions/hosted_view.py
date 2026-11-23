"""Retain pushed evidence, attempt observations, chart projections and reports."""

from alembic import op

revision = "hosted_view"
down_revision = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE runs (
            address text PRIMARY KEY,
            experiment text NOT NULL,
            kind text,
            job text,
            latest_attempt_number integer NOT NULL,
            accepted_rev bigint NOT NULL DEFAULT 0,
            published_rev bigint NOT NULL DEFAULT 0,
            last_push_at timestamptz NOT NULL DEFAULT now(),
            projection_error text,
            outcome_attempt integer,
            file_state text,
            attempt_state text,
            state text GENERATED ALWAYS AS (
                CASE
                    WHEN attempt_state NOT IN ('running', 'setting_up') THEN attempt_state
                    WHEN outcome_attempt = latest_attempt_number AND file_state <> 'running' THEN file_state
                    ELSE coalesce(attempt_state, 'running')
                END
            ) STORED,
            machine text,
            attempt_position text,
            position text,
            headline text,
            latest text,
            panel_order integer,
            run_clock text,
            panel_definition text
        )
    """)
    op.execute("""
        CREATE INDEX pending_projections ON runs(address) WHERE published_rev < accepted_rev
    """)
    op.execute("CREATE INDEX experiment_runs ON runs(experiment,address)")
    op.execute("""
        CREATE INDEX open_experiments ON runs(last_push_at,experiment)
        WHERE state IN ('running','setting_up')
    """)
    op.execute("""
        CREATE TABLE run_files (
            address text NOT NULL REFERENCES runs(address) ON DELETE CASCADE,
            key text NOT NULL, sha256 text NOT NULL, body bytea NOT NULL,
            attempt_number integer NOT NULL, modified_at timestamptz NOT NULL,
            writer text, seq_first bigint, seq_last bigint, level bigint,
            visible boolean NOT NULL,
            PRIMARY KEY (address, key)
        )
    """)
    op.execute("""
        CREATE INDEX run_writer_blocks ON run_files(address, writer, seq_first, seq_last)
        WHERE writer IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE run_charts (
            address text PRIMARY KEY REFERENCES runs(address) ON DELETE CASCADE,
            overview text NOT NULL, payload text NOT NULL, checkpoints text
        )
    """)
    op.execute("""
        CREATE TABLE attempts (
            address text NOT NULL REFERENCES runs(address) ON DELETE CASCADE,
            number integer NOT NULL,
            attempt_id text NOT NULL UNIQUE,
            producer_seq bigint NOT NULL,
            console_position bigint NOT NULL,
            finished boolean NOT NULL,
            summary text NOT NULL,
            observation text NOT NULL,
            PRIMARY KEY (address, number)
        )
    """)
    op.execute("""
        CREATE TABLE reports (
            experiment text PRIMARY KEY,
            html text NOT NULL,
            built_at timestamptz NOT NULL,
            declaration text NOT NULL,
            headline text,
            runs_label text NOT NULL
        )
    """)
