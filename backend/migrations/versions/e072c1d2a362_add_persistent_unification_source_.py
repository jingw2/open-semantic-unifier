"""add persistent unification source identity"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e072c1d2a362'
down_revision: Union[str, None] = '3a5d3f7c8e11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add only the new feature tables; preserve existing application schema.
    op.create_table('unified_entities',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('project_id', sa.String(length=32), nullable=False),
    sa.Column('entity_type', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_unified_entities_project_id'), 'unified_entities', ['project_id'], unique=False)
    op.create_table('unified_source_records',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('project_id', sa.String(length=32), nullable=False),
    sa.Column('source_system', sa.String(length=160), nullable=False),
    sa.Column('dataset', sa.String(length=200), nullable=False),
    sa.Column('source_record_id', sa.String(length=320), nullable=False),
    sa.Column('current_entity_id', sa.String(length=32), nullable=False),
    sa.Column('current_revision_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'source_system', 'dataset', 'source_record_id', name='uq_source_record_identity')
    )
    op.create_index(op.f('ix_unified_source_records_current_entity_id'), 'unified_source_records', ['current_entity_id'], unique=False)
    op.create_index(op.f('ix_unified_source_records_project_id'), 'unified_source_records', ['project_id'], unique=False)
    op.create_table('unified_source_revisions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('source_record_id', sa.String(length=32), nullable=False),
    sa.Column('revision_no', sa.Integer(), nullable=False),
    sa.Column('input_version', sa.String(length=160), nullable=False),
    sa.Column('attributes', sa.JSON(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['source_record_id'], ['unified_source_records.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_record_id', 'revision_no', name='uq_source_revision_number'),
    sa.UniqueConstraint('id', 'source_record_id', name='uq_source_revision_source_pair')
    )
    op.create_index(op.f('ix_unified_source_revisions_source_record_id'), 'unified_source_revisions', ['source_record_id'], unique=False)
    # Add cross-table references only after both sides exist. SQLite needs a table rebuild.
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('unified_source_records', recreate='always') as batch_op:
            batch_op.create_foreign_key('fk_source_record_entity', 'unified_entities', ['current_entity_id'], ['id'])
            batch_op.create_foreign_key(
                'fk_current_revision_same_source', 'unified_source_revisions',
                ['current_revision_id', 'id'], ['id', 'source_record_id'],
            )
    else:
        op.create_foreign_key('fk_source_record_entity', 'unified_source_records', 'unified_entities', ['current_entity_id'], ['id'])
        op.create_foreign_key(
            'fk_current_revision_same_source', 'unified_source_records', 'unified_source_revisions',
            ['current_revision_id', 'id'], ['id', 'source_record_id'],
        )



def downgrade() -> None:
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('unified_source_records', recreate='always') as batch_op:
            batch_op.drop_constraint('fk_current_revision_same_source', type_='foreignkey')
            batch_op.drop_constraint('fk_source_record_entity', type_='foreignkey')
    else:
        op.drop_constraint('fk_current_revision_same_source', 'unified_source_records', type_='foreignkey')
        op.drop_constraint('fk_source_record_entity', 'unified_source_records', type_='foreignkey')
    op.drop_index(op.f('ix_unified_source_revisions_source_record_id'), table_name='unified_source_revisions')
    op.drop_index(op.f('ix_unified_source_records_current_entity_id'), table_name='unified_source_records')
    op.drop_index(op.f('ix_unified_source_records_project_id'), table_name='unified_source_records')
    op.drop_index(op.f('ix_unified_entities_project_id'), table_name='unified_entities')
    op.drop_table('unified_source_revisions')
    op.drop_table('unified_source_records')
    op.drop_table('unified_entities')
