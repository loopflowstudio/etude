"""Index experiment summaries, publish complete eval blocks and retain built reports."""

from alembic import op

revision = "shared_experiments"
down_revision = "attempt_observations"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE runs
        ADD COLUMN experiment text NOT NULL DEFAULT '',
        ADD COLUMN kind text,
        ADD COLUMN job text,
        ADD COLUMN file_state text,
        ADD COLUMN attempt_state text,
        ADD COLUMN machine text,
        ADD COLUMN attempt_position text,
        ADD COLUMN position text,
        ADD COLUMN headline text,
        ADD COLUMN latest text,
        ADD COLUMN panel_order integer,
        ADD COLUMN run_clock text,
        ADD COLUMN panel_definition text
    """)
    op.execute("""
        UPDATE runs SET experiment=split_part(split_part(address, '/', 1), '@', 1),
            published_rev=0
    """)
    op.execute("CREATE INDEX experiment_runs ON runs(experiment,address)")
    op.execute("""
        CREATE INDEX open_experiments ON runs(last_push_at,experiment)
        WHERE coalesce(attempt_state,file_state,'running') IN ('running','setting_up')
    """)
    op.execute("ALTER TABLE run_files ADD COLUMN visible boolean NOT NULL DEFAULT true")
    op.execute("""
        UPDATE runs SET attempt_state=coalesce(observation::jsonb->>'outcome',observation::jsonb->>'phase'),
            machine=observation::jsonb#>>'{context,machine}'
        FROM attempts WHERE attempts.address=runs.address AND attempts.number=runs.latest_attempt_number
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
