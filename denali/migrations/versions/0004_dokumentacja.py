"""Dokumentacja i log problemów: runbooki, procedury (kroki, kontakty), pliki, skarbiec, incydenty z przebiegiem.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('contact',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('phone', sa.String(length=40), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contact'))
    )
    op.create_table('procedure',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('intro_md', sa.Text(), nullable=False),
    sa.Column('trigger', sa.String(length=100), nullable=False),
    sa.Column('last_tested_at', sa.Float(), nullable=True),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_procedure'))
    )
    op.create_table('runbook',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body_md', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runbook'))
    )
    op.create_table('procedure_contact',
    sa.Column('procedure_id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['contact_id'], ['contact.id'], name=op.f('fk_procedure_contact_contact_id_contact'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['procedure_id'], ['procedure.id'], name=op.f('fk_procedure_contact_procedure_id_procedure'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('procedure_id', 'contact_id', name=op.f('pk_procedure_contact'))
    )
    op.create_table('procedure_step',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('procedure_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body_md', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['procedure_id'], ['procedure.id'], name=op.f('fk_procedure_step_procedure_id_procedure'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_procedure_step'))
    )
    with op.batch_alter_table('procedure_step', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_procedure_step_procedure_id'), ['procedure_id'], unique=False)

    op.create_table('file',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('mime', sa.String(length=100), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('path', sa.String(length=100), nullable=False),
    sa.Column('uploaded_at', sa.Float(), nullable=False),
    sa.Column('uploaded_by', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['device.id'], name=op.f('fk_file_device_id_device'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_file')),
    sa.UniqueConstraint('path', name=op.f('uq_file_path'))
    )
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_file_device_id'), ['device_id'], unique=False)

    op.create_table('incident',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('root_device_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.Float(), nullable=False),
    sa.Column('ended_at', sa.Float(), nullable=True),
    sa.Column('acked_by', sa.String(length=64), nullable=False),
    sa.Column('cause_md', sa.Text(), nullable=False),
    sa.Column('fix_md', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['root_device_id'], ['device.id'], name=op.f('fk_incident_root_device_id_device'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_incident'))
    )
    with op.batch_alter_table('incident', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_root_device_id'), ['root_device_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_incident_started_at'), ['started_at'], unique=False)

    op.create_table('procedure_device',
    sa.Column('procedure_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['device.id'], name=op.f('fk_procedure_device_device_id_device'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['procedure_id'], ['procedure.id'], name=op.f('fk_procedure_device_procedure_id_procedure'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('procedure_id', 'device_id', name=op.f('pk_procedure_device'))
    )
    with op.batch_alter_table('procedure_device', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_procedure_device_device_id'), ['device_id'], unique=False)

    op.create_table('runbook_device',
    sa.Column('runbook_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['device.id'], name=op.f('fk_runbook_device_device_id_device'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['runbook_id'], ['runbook.id'], name=op.f('fk_runbook_device_runbook_id_runbook'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('runbook_id', 'device_id', name=op.f('pk_runbook_device'))
    )
    with op.batch_alter_table('runbook_device', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runbook_device_device_id'), ['device_id'], unique=False)

    op.create_table('vault_secret',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=True),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('username', sa.String(length=200), nullable=False),
    sa.Column('ciphertext', sa.Text(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['device.id'], name=op.f('fk_vault_secret_device_id_device'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vault_secret'))
    )
    with op.batch_alter_table('vault_secret', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_vault_secret_device_id'), ['device_id'], unique=False)

    op.create_table('incident_device',
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['device.id'], name=op.f('fk_incident_device_device_id_device'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], name=op.f('fk_incident_device_incident_id_incident'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('incident_id', 'device_id', name=op.f('pk_incident_device'))
    )
    with op.batch_alter_table('incident_device', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_device_device_id'), ['device_id'], unique=False)

    op.create_table('incident_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.Float(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('user', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], name=op.f('fk_incident_event_incident_id_incident'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_incident_event'))
    )
    with op.batch_alter_table('incident_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_event_incident_id'), ['incident_id'], unique=False)



def downgrade():
    with op.batch_alter_table('incident_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_event_incident_id'))

    op.drop_table('incident_event')
    with op.batch_alter_table('incident_device', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_device_device_id'))

    op.drop_table('incident_device')
    with op.batch_alter_table('vault_secret', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_vault_secret_device_id'))

    op.drop_table('vault_secret')
    with op.batch_alter_table('runbook_device', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runbook_device_device_id'))

    op.drop_table('runbook_device')
    with op.batch_alter_table('procedure_device', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_procedure_device_device_id'))

    op.drop_table('procedure_device')
    with op.batch_alter_table('incident', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_started_at'))
        batch_op.drop_index(batch_op.f('ix_incident_root_device_id'))

    op.drop_table('incident')
    with op.batch_alter_table('file', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_file_device_id'))

    op.drop_table('file')
    with op.batch_alter_table('procedure_step', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_procedure_step_procedure_id'))

    op.drop_table('procedure_step')
    op.drop_table('procedure_contact')
    op.drop_table('runbook')
    op.drop_table('procedure')
    op.drop_table('contact')
