"""ML pipeline: tenant training policy, training examples, dataset membership, immutable evaluation runs, registry lifecycle

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

JSONT = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')


def upgrade() -> None:
    bind = op.get_bind()
    # -- preprocessing_version now encodes the enabled ops (for example 1+ops1:autocontrast,deskew): 16 chars is too short
    op.alter_column('ocr_results', 'preprocessing_version', type_=sa.String(64), existing_type=sa.String(16), existing_nullable=False,
                    existing_server_default='1')
    # -- tenant consent
    op.add_column('clients', sa.Column('training_policy', sa.String(16), server_default='none', nullable=False))
    op.add_column('clients', sa.Column('training_policy_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('clients', sa.Column('training_policy_updated_by', sa.String(100), nullable=True))
    op.add_column('clients', sa.Column('external_processing', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.execute("UPDATE clients SET training_policy='pooled' WHERE training_opt_in")   # preserve the existing consent meaning

    # -- registry lifecycle rename (evaluating -> evaluated, active -> production)
    op.drop_index('uq_model_versions_one_active', table_name='model_versions')
    op.execute("UPDATE model_versions SET status='evaluated' WHERE status='evaluating'")
    op.execute("UPDATE model_versions SET status='production' WHERE status='active'")
    op.create_index('uq_model_versions_one_active', 'model_versions', ['kind'], unique=True,
                    postgresql_where=sa.text("status = 'production'"), sqlite_where=sa.text("status = 'production'"))
    op.add_column('model_versions', sa.Column('schema_version', sa.String(16), nullable=True))
    op.add_column('model_versions', sa.Column('artifact_sha256', sa.String(64), nullable=True))
    op.add_column('model_versions', sa.Column('signature', sa.String(128), nullable=True))
    op.add_column('model_versions', sa.Column('config', JSONT, server_default='{}', nullable=False))
    op.add_column('model_versions', sa.Column('training_meta', JSONT, server_default='{}', nullable=False))
    op.add_column('model_versions', sa.Column('status_reason', sa.String(500), nullable=True))

    # -- datasets
    op.add_column('training_datasets', sa.Column('name', sa.String(100), nullable=True))
    op.add_column('training_datasets', sa.Column('kind', sa.String(16), server_default='training', nullable=False))
    op.add_column('training_datasets', sa.Column('scope', sa.String(80), server_default='pooled', nullable=False))
    op.add_column('training_datasets', sa.Column('status', sa.String(24), server_default='active', nullable=False))
    op.add_column('training_datasets', sa.Column('schema_version', sa.String(16), nullable=True))
    op.add_column('training_datasets', sa.Column('split_spec', JSONT, server_default='{}', nullable=False))
    op.add_column('training_datasets', sa.Column('created_by', sa.String(100), nullable=True))

    op.create_table(
        'training_examples',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('client_id', sa.String(64), nullable=False),
        sa.Column('process_id', sa.String(64), nullable=False),
        sa.Column('schema_version', sa.String(16), nullable=False),
        sa.Column('truth', JSONT, nullable=False),
        sa.Column('source', sa.String(24), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('quality_report', JSONT, nullable=False),
        sa.Column('machine_result_sha256', sa.String(64), nullable=False),
        sa.Column('content_sha256', sa.String(64), nullable=False),
        sa.Column('group_key', sa.String(64), nullable=False),
        sa.Column('batch_id', sa.String(64), nullable=True),
        sa.Column('annotator', sa.String(100), nullable=True),
        sa.Column('reviewed_by', sa.String(100), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('policy_at_creation', sa.String(16), nullable=False),
        sa.Column('ocr_model_version_id', sa.String(64), sa.ForeignKey('model_versions.id'), nullable=True),
        sa.Column('extractor_model_version_id', sa.String(64), sa.ForeignKey('model_versions.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['client_id', 'process_id'], ['processes.client_id', 'processes.id'],
                                ondelete='CASCADE', name='fk_training_examples_tenant_process'),
        sa.UniqueConstraint('client_id', 'process_id', name='uq_training_examples_process'),
    )
    op.create_index('ix_training_examples_client_status', 'training_examples', ['client_id', 'status'])
    op.create_index('ix_training_examples_group', 'training_examples', ['group_key'])

    op.create_table(
        'dataset_examples',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('dataset_id', sa.String(64), sa.ForeignKey('training_datasets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('example_id', sa.String(64), sa.ForeignKey('training_examples.id', ondelete='SET NULL'), nullable=True),
        sa.Column('split', sa.String(8), nullable=False),
        sa.Column('group_key', sa.String(64), nullable=False),
    )
    op.create_index('ix_dataset_examples_dataset', 'dataset_examples', ['dataset_id'])
    op.create_index('ix_dataset_examples_example', 'dataset_examples', ['example_id'])

    op.create_table(
        'evaluation_runs',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('model_version_id', sa.String(64), sa.ForeignKey('model_versions.id'), nullable=False),
        sa.Column('dataset_id', sa.String(64), sa.ForeignKey('training_datasets.id'), nullable=True),
        sa.Column('dataset_ref', sa.String(200), nullable=False),
        sa.Column('dataset_sha256', sa.String(64), nullable=False),
        sa.Column('split', sa.String(8), nullable=False),
        sa.Column('sample_count', sa.Integer, nullable=False),
        sa.Column('metrics', JSONT, nullable=False),
        sa.Column('slices', JSONT, nullable=False),
        sa.Column('config', JSONT, nullable=False),
        sa.Column('environment', JSONT, nullable=False),
        sa.Column('synthetic', sa.Boolean, nullable=False),
        sa.Column('result_sha256', sa.String(64), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_evaluation_runs_model', 'evaluation_runs', ['model_version_id'])
    if bind.dialect.name == 'postgresql':
        op.execute("""CREATE FUNCTION evaluation_runs_immutable() RETURNS trigger AS $$
                      BEGIN RAISE EXCEPTION 'evaluation_runs are immutable'; END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER evaluation_runs_no_change BEFORE UPDATE OR DELETE ON evaluation_runs
                      FOR EACH ROW EXECUTE FUNCTION evaluation_runs_immutable()""")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("DROP TRIGGER IF EXISTS evaluation_runs_no_change ON evaluation_runs")
        op.execute("DROP FUNCTION IF EXISTS evaluation_runs_immutable()")
    op.drop_table('evaluation_runs')
    op.drop_table('dataset_examples')
    op.drop_table('training_examples')
    for c in ('created_by', 'split_spec', 'schema_version', 'status', 'scope', 'kind', 'name'):
        op.drop_column('training_datasets', c)
    op.drop_index('uq_model_versions_one_active', table_name='model_versions')
    op.execute("UPDATE model_versions SET status='active' WHERE status='production'")
    op.execute("UPDATE model_versions SET status='evaluating' WHERE status='evaluated'")
    op.create_index('uq_model_versions_one_active', 'model_versions', ['kind'], unique=True,
                    postgresql_where=sa.text("status = 'active'"), sqlite_where=sa.text("status = 'active'"))
    for c in ('status_reason', 'training_meta', 'config', 'signature', 'artifact_sha256', 'schema_version'):
        op.drop_column('model_versions', c)
    for c in ('external_processing', 'training_policy_updated_by', 'training_policy_updated_at', 'training_policy'):
        op.drop_column('clients', c)
