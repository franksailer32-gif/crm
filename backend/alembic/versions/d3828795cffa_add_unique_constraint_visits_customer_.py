"""add_unique_constraint_visits_customer_date_planned

Revision ID: d3828795cffa
Revises: 60d899a898d3
Create Date: 2026-04-03 11:16:13.647858

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3828795cffa'
down_revision: Union[str, None] = '60d899a898d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add a DB-level unique constraint to guarantee that a customer
    # cannot have more than one planned visit on the same date.
    op.execute(
        "CREATE UNIQUE INDEX uq_visits_customer_scheduled_date_planned "
        "ON visits (customer_id, scheduled_date) "
        "WHERE status = 'planned';"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_visits_customer_scheduled_date_planned;")
