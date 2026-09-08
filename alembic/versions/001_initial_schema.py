"""Initial migration for known_persons and activity_events tables

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-09-08 21:38:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create known_persons table
    op.create_table(
        'known_persons',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('person_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('tag', sa.String(length=64), nullable=False, server_default='Authorized'),
        sa.Column('face_descriptor', sa.JSON(), nullable=True),
        sa.Column('snapshot_path', sa.String(length=255), nullable=True),
        sa.Column('snapshot_base64', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_known_persons_id'), 'known_persons', ['id'], unique=False)
    op.create_index(op.f('ix_known_persons_name'), 'known_persons', ['name'], unique=False)
    op.create_index(op.f('ix_known_persons_person_id'), 'known_persons', ['person_id'], unique=True)

    # 2. Create activity_events table
    op.create_table(
        'activity_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=32), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('mode', sa.String(length=64), nullable=False, server_default='DIRECT_ROOM_SURVEILLANCE'),
        sa.Column('pane_id', sa.String(length=64), nullable=True),
        sa.Column('bounding_boxes', sa.JSON(), nullable=True),
        sa.Column('image_path', sa.String(length=255), nullable=True),
        sa.Column('thumbnail_base64', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_activity_events_id'), 'activity_events', ['id'], unique=False)
    op.create_index(op.f('ix_activity_events_event_id'), 'activity_events', ['event_id'], unique=True)
    op.create_index(op.f('ix_activity_events_timestamp'), 'activity_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_activity_events_event_type'), 'activity_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_activity_events_severity'), 'activity_events', ['severity'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_activity_events_severity'), table_name='activity_events')
    op.drop_index(op.f('ix_activity_events_event_type'), table_name='activity_events')
    op.drop_index(op.f('ix_activity_events_timestamp'), table_name='activity_events')
    op.drop_index(op.f('ix_activity_events_event_id'), table_name='activity_events')
    op.drop_index(op.f('ix_activity_events_id'), table_name='activity_events')
    op.drop_table('activity_events')

    op.drop_index(op.f('ix_known_persons_person_id'), table_name='known_persons')
    op.drop_index(op.f('ix_known_persons_name'), table_name='known_persons')
    op.drop_index(op.f('ix_known_persons_id'), table_name='known_persons')
    op.drop_table('known_persons')
