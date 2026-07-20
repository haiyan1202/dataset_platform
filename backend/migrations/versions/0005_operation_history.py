"""add operation history

Revision ID: 0005_operation_history
Revises: 0004_upload_idempotency
Create Date: 2026-07-17
"""

from alembic import op

from app.models.entities import OperationHistory

revision = "0005_operation_history"
down_revision = "0004_upload_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    OperationHistory.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    OperationHistory.__table__.drop(bind=op.get_bind(), checkfirst=True)