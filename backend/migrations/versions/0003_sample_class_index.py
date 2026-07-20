"""add sample class indexes

Revision ID: 0003_sample_class_index
Revises: 0002_keypoints
Create Date: 2026-07-16
"""

from alembic import op

from app.models.entities import SampleClassIndex

revision = "0003_sample_class_index"
down_revision = "0002_keypoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    SampleClassIndex.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    SampleClassIndex.__table__.drop(bind=op.get_bind(), checkfirst=True)
