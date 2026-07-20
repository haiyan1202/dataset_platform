"""store byte counts as bigint

Revision ID: 0006_bigint_byte_counts
Revises: 0005_operation_history
Create Date: 2026-07-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_bigint_byte_counts"
down_revision = "0005_operation_history"
branch_labels = None
depends_on = None


def _column_is_bigint(table_name: str, column_name: str) -> bool:
    column = next(item for item in sa.inspect(op.get_bind()).get_columns(table_name) if item["name"] == column_name)
    return isinstance(column["type"], sa.BigInteger)


def upgrade() -> None:
    for table_name, column_name in (
        ("organizations", "storage_quota_bytes"),
        ("upload_sessions", "size_bytes"),
        ("assets", "size_bytes"),
    ):
        if not _column_is_bigint(table_name, column_name):
            op.alter_column(
                table_name,
                column_name,
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                postgresql_using=f"{column_name}::bigint",
            )


def downgrade() -> None:
    for table_name, column_name in (
        ("organizations", "storage_quota_bytes"),
        ("upload_sessions", "size_bytes"),
        ("assets", "size_bytes"),
    ):
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            postgresql_using=f"{column_name}::integer",
        )
