"""Keep accepted run evidence separate from the last complete chart."""

from alembic import op

revision = "training_curves"
down_revision = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE runs (
            address text PRIMARY KEY,
            accepted_rev bigint NOT NULL DEFAULT 0,
            published_rev bigint NOT NULL DEFAULT 0,
            last_push_at timestamptz NOT NULL DEFAULT now(),
            projection_error text
        )
    """)
    op.execute("""
        CREATE INDEX pending_projections ON runs(address) WHERE published_rev < accepted_rev
    """)
    op.execute("""
        CREATE TABLE run_files (
            address text NOT NULL REFERENCES runs(address) ON DELETE CASCADE,
            key text NOT NULL, sha256 text NOT NULL, body bytea NOT NULL,
            attempt_number integer NOT NULL, modified_at timestamptz NOT NULL,
            writer text, seq_first bigint, seq_last bigint, level bigint,
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
            overview text NOT NULL, payload text NOT NULL
        )
    """)
