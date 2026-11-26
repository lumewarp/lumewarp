"""alerts and usage diagnostics

Revision ID: 0003
Revises: 0002

Adds the Alertmanager receiver's tables: `incidents` (one outage, grouping the alerts of
one alertname, zone and target set whose intervals overlap), `alerts` (one row per
fingerprint and start, never deleted, each in one incident) and `alert_targets` (the
catalog entries or upstreams an alert resolved to). Also adds the two nullable diagnostics on `usage_events` (`error_code`,
`ttft_ms`); NULL means unknown, so old rows need no backfill.

Downgrades are not supported (restore a database backup instead).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("usage_events", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.add_column("usage_events", sa.Column("ttft_ms", sa.Integer(), nullable=True))

    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("alertname", sa.String(length=256), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=True),
        sa.Column("group_key", sa.String(length=64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=64), nullable=True),
        sa.Column("lumewarp_status", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('firing', 'resolved')", name=op.f("ck_incidents_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index("ix_incidents_starts_at_id", "incidents", ["starts_at", "id"], unique=False)
    op.create_index(
        "ix_incidents_status_starts_at", "incidents", ["status", "starts_at"], unique=False
    )
    op.create_index(
        "ix_incidents_zone_id_starts_at", "incidents", ["zone_id", "starts_at"], unique=False
    )
    op.create_index(
        "ix_incidents_group", "incidents", ["alertname", "group_key", "starts_at"], unique=False
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("alertname", sa.String(length=256), nullable=False),
        sa.Column("severity", sa.String(length=64), nullable=True),
        sa.Column("zone_id", sa.String(length=64), nullable=True),
        sa.Column("lumewarp_status", sa.String(length=64), nullable=True),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("annotations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generator_url", sa.Text(), nullable=True),
        sa.Column(
            "first_received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('firing', 'resolved')", name=op.f("ck_alerts_status")),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name=op.f("fk_alerts_incident_id_incidents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
        sa.UniqueConstraint(
            "fingerprint", "starts_at", name=op.f("uq_alerts_fingerprint_starts_at")
        ),
    )
    op.create_index(op.f("ix_alerts_incident_id"), "alerts", ["incident_id"], unique=False)
    op.create_index(op.f("ix_alerts_alertname"), "alerts", ["alertname"], unique=False)
    op.create_index(op.f("ix_alerts_starts_at"), "alerts", ["starts_at"], unique=False)
    op.create_index("ix_alerts_status_starts_at", "alerts", ["status", "starts_at"], unique=False)
    op.create_index("ix_alerts_zone_id_starts_at", "alerts", ["zone_id", "starts_at"], unique=False)

    op.create_table(
        "alert_targets",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.CheckConstraint(
            "kind IN ('model', 'alias', 'router', 'upstream')",
            name=op.f("ck_alert_targets_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_alert_targets_alert_id_alerts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("alert_id", "kind", "target_id", name=op.f("pk_alert_targets")),
    )
    op.create_index(
        "ix_alert_targets_kind_target_id", "alert_targets", ["kind", "target_id"], unique=False
    )
    op.create_index("ix_alert_targets_name", "alert_targets", ["name"], unique=False)


def downgrade() -> None:
    """Downgrades are not supported: restore a database backup instead."""
    raise NotImplementedError("restore a database backup")
