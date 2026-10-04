"""hardening columns and indexes

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ocr_results', sa.Column('preprocessing_version', sa.String(length=16), server_default='1', nullable=False))
    op.add_column('plans', sa.Column('bulk_requests_per_minute', sa.Integer(), server_default='10', nullable=False))
    op.add_column('plans', sa.Column('documents_per_minute', sa.Integer(), server_default='600', nullable=False))
    op.add_column('plans', sa.Column('max_pages_per_document', sa.Integer(), server_default='20', nullable=False))
    op.add_column('plans', sa.Column('max_running_jobs', sa.Integer(), server_default='4', nullable=False))
    op.add_column('processes', sa.Column('request_id', sa.String(length=64), nullable=True))
    op.drop_index(op.f('ix_processes_status_updated'), table_name='processes')
    op.create_index('ix_processes_client_processing', 'processes', ['client_id'], unique=False, postgresql_where=sa.text("status = 'processing'"))
    op.create_index('ix_processes_processing_started', 'processes', ['started_at'], unique=False, postgresql_where=sa.text("status = 'processing'"))
    op.create_index('ix_processes_queued_enqueued', 'processes', ['enqueued_at'], unique=False, postgresql_where=sa.text("status = 'queued'"))
    # Per-plan defaults for the new governance columns (tunable later with plain UPDATEs).
    for plan, bulk, docs, pages, running in (("free", 5, 60, 10, 2), ("pro", 30, 600, 20, 8), ("enterprise", 120, 3000, 50, 32)):
        op.execute(sa.text("UPDATE plans SET bulk_requests_per_minute=:b, documents_per_minute=:d, "
                           "max_pages_per_document=:p, max_running_jobs=:r WHERE id=:id").bindparams(b=bulk, d=docs, p=pages, r=running, id=plan))


def downgrade() -> None:
    op.drop_index('ix_processes_queued_enqueued', table_name='processes', postgresql_where=sa.text("status = 'queued'"))
    op.drop_index('ix_processes_processing_started', table_name='processes', postgresql_where=sa.text("status = 'processing'"))
    op.drop_index('ix_processes_client_processing', table_name='processes', postgresql_where=sa.text("status = 'processing'"))
    op.create_index(op.f('ix_processes_status_updated'), 'processes', ['status', 'updated_at'], unique=False)
    op.drop_column('processes', 'request_id')
    op.drop_column('plans', 'max_running_jobs')
    op.drop_column('plans', 'max_pages_per_document')
    op.drop_column('plans', 'documents_per_minute')
    op.drop_column('plans', 'bulk_requests_per_minute')
    op.drop_column('ocr_results', 'preprocessing_version')
