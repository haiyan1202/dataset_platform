"""add dataset recycle-bin timestamp

Revision ID: 0007_dataset_soft_delete
Revises: 0006_bigint_byte_counts
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_dataset_soft_delete"
down_revision = "0006_bigint_byte_counts"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    if not _has_column("datasets", "deleted_at"):
        op.add_column(
            "datasets", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    if _has_column("datasets", "deleted_at"):
        op.drop_column("datasets", "deleted_at")
