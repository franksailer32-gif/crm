"""Add use_new_pricing to subscriptions

Revision ID: f88d3e21a7b4
Revises: fb021a02daf1
Create Date: 2026-04-30 07:48:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f88d3e21a7b4'
down_revision: Union[str, None] = 'fb021a02daf1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add use_new_pricing column to subscriptions table
    op.add_column('subscriptions', sa.Column('use_new_pricing', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    # Remove use_new_pricing column from subscriptions table
    op.drop_column('subscriptions', 'use_new_pricing')
