"""one liability per account

Revision ID: c0254e5aab74
Revises: 977e2552820c
Create Date: 2026-10-08 10:01:07.456396

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0254e5aab74'
down_revision: Union[str, Sequence[str], None] = '977e2552820c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# liabilities are upserted by account_id, so make the existing index unique.
# fails if dev.db already holds two liabilities for one account; none exist before this ticket.
def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('liabilities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_liabilities_account_id'))
        batch_op.create_index(batch_op.f('ix_liabilities_account_id'), ['account_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('liabilities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_liabilities_account_id'))
        batch_op.create_index(batch_op.f('ix_liabilities_account_id'), ['account_id'], unique=False)
