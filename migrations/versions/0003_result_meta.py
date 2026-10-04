"""internal result metadata (evidence, stage versions, page results)

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ocr_results', sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'),
                                           server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('ocr_results', 'meta')
