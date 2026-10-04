"""human review record: one row per reviewed document (confirmed as extracted, or corrected)

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'reviews',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('submission_id', sa.String(64), nullable=False),
        sa.Column('client_id', sa.String(64), nullable=False),
        sa.Column('process_id', sa.String(64), nullable=False),
        sa.Column('outcome', sa.String(16), nullable=False),
        sa.Column('api_key_id', sa.String(64), sa.ForeignKey('api_keys.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['client_id', 'process_id'], ['processes.client_id', 'processes.id'],
                                ondelete='CASCADE', name='fk_reviews_tenant_process'),
        sa.UniqueConstraint('client_id', 'process_id', name='uq_reviews_client_process'),
    )
    op.create_index('ix_reviews_client_created', 'reviews', ['client_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_reviews_client_created', table_name='reviews')
    op.drop_table('reviews')
