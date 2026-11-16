"""Encrypted state, durable queues and single worker lease."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
    )
    op.create_table(
        "telegram_inbox",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("done", sa.Integer, nullable=False),
        sa.Column("created", sa.Float, nullable=False),
    )
    op.create_index("ix_telegram_inbox_done", "telegram_inbox", ["done"])
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("due", sa.Float, nullable=False),
        sa.Column("sent", sa.Integer, nullable=False),
    )
    op.create_index("ix_notification_outbox_due", "notification_outbox", ["due"])
    op.create_index("ix_notification_outbox_sent", "notification_outbox", ["sent"])
    op.create_table(
        "worker_lease",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("owner", sa.String(40), nullable=False),
        sa.Column("expires", sa.Float, nullable=False),
    )


def downgrade():
    for name in ("worker_lease", "notification_outbox", "telegram_inbox", "app_state"):
        op.drop_table(name)
