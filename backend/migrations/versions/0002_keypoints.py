"""add keypoint definitions

Revision ID: 0002_keypoints
Revises: 0001_initial
Create Date: 2026-07-16
"""

from alembic import op

from app.models.entities import KeypointDefinition

revision = "0002_keypoints"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    KeypointDefinition.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    KeypointDefinition.__table__.drop(bind=op.get_bind(), checkfirst=True)
