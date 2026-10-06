"""timezone-aware timestamps

Revision ID: 977e2552820c
Revises: e1f04fa8eb57
Create Date: 2026-10-06 15:34:17.807078

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '977e2552820c'
down_revision: Union[str, Sequence[str], None] = 'e1f04fa8eb57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Written by hand: sqlite doesn't store the timezone flag, so autogenerate
# can't detect this change. On sqlite it's storage-neutral; on postgres it
# turns these columns into timestamptz.
TIMESTAMP_COLUMNS = [
    ("users", "created_at"),
    ("plaid_items", "created_at"),
    ("accounts", "updated_at"),
]


def upgrade() -> None:
    """Upgrade schema."""
    for table, column in TIMESTAMP_COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.DateTime(),
                type_=sa.DateTime(timezone=True),
                existing_nullable=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    for table, column in reversed(TIMESTAMP_COLUMNS):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.DateTime(timezone=True),
                type_=sa.DateTime(),
                existing_nullable=False,
            )
