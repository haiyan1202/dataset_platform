"""add upload-session idempotency key

Revision ID: 0004_upload_idempotency
Revises: 0003_sample_class_index
Create Date: 2026-07-16
"""

from alembic import context, op
from sqlalchemy import inspect

revision = "0004_upload_idempotency"
down_revision = "0003_sample_class_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial migration is metadata-based for fresh local installs and already has
    # this column; existing installations need the guarded runtime alteration.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("upload_sessions")}
    if "idempotency_key" not in columns:
        op.execute("ALTER TABLE upload_sessions ADD COLUMN idempotency_key VARCHAR(255)")
        op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_upload_sessions_idempotency_key ON upload_sessions (idempotency_key)")


def downgrade() -> None:
    # SQLite and PostgreSQL differ in DROP COLUMN support; preserve historical keys on downgrade.
    pass
