"""Add is_poi column to known_persons table

Revision ID: 002_add_is_poi
Revises: 001_initial_schema
Create Date: 2026-09-15 15:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '002_add_is_poi'
down_revision: Union[str, None] = '001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('known_persons', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_poi', sa.Boolean(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('known_persons', schema=None) as batch_op:
        batch_op.drop_column('is_poi')
