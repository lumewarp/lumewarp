"""Free the model name and distinguish API endpoints from routing resources.

Revision ID: 0007
Revises: 0006

Config versions contain binary protobuf, not JSON: unchanged field numbers preserve
rollback and persisted gateway snapshots without conversion or deletion.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("routes", "model", new_column_name="name")
    op.execute("ALTER TABLE routes RENAME CONSTRAINT uq_routes_model TO uq_routes_name")
    op.alter_column("usage_events", "model", new_column_name="requested")
    op.alter_column("usage_events", "route", new_column_name="endpoint")


def downgrade() -> None:
    op.alter_column("usage_events", "endpoint", new_column_name="route")
    op.alter_column("usage_events", "requested", new_column_name="model")
    op.execute("ALTER TABLE routes RENAME CONSTRAINT uq_routes_name TO uq_routes_model")
    op.alter_column("routes", "name", new_column_name="model")
