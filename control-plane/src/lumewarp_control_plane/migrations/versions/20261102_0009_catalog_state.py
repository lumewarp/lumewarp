"""Add the ID-preserving routing rollback sidecar.

Revision ID: 0009
Revises: 0008

This is a preparatory migration, not the catalog table conversion. Existing snapshots
remain readable with a NULL sidecar until that conversion clears incompatible versions.
Downgrade drops sidecars and restores the previous immutability trigger.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Explicit immutable metadata checks also reject changes to future non-compaction
# columns. Only the payload and sidecar can be removed, and only in the same update.
IMMUTABLE = """
CREATE OR REPLACE FUNCTION lumewarp_config_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['payload', 'catalog_state', 'compacted_at'])
        IS DISTINCT FROM
        (to_jsonb(OLD) - ARRAY['payload', 'catalog_state', 'compacted_at'])
        OR (NEW.payload IS DISTINCT FROM OLD.payload AND NEW.payload IS NOT NULL)
        OR (NEW.catalog_state IS DISTINCT FROM OLD.catalog_state
            AND NOT (NEW.catalog_state IS NULL AND NEW.payload IS NULL
                     AND NEW.compacted_at IS NOT NULL))
        OR (NEW.payload IS NULL AND NEW.catalog_state IS NOT NULL)
        OR (OLD.compacted_at IS NOT NULL
            AND NEW.compacted_at IS DISTINCT FROM OLD.compacted_at)
    THEN
        RAISE EXCEPTION 'config_versions rows are immutable (only compaction is allowed)'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
"""


def upgrade() -> None:
    op.add_column(
        "config_versions",
        sa.Column("catalog_state", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_config_versions_catalog_state_compaction"),
        "config_versions",
        "payload IS NOT NULL OR catalog_state IS NULL",
    )
    op.execute(IMMUTABLE)


def downgrade() -> None:
    op.execute(
        """
CREATE OR REPLACE FUNCTION lumewarp_config_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['payload', 'compacted_at'])
        IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['payload', 'compacted_at'])
        OR (NEW.payload IS DISTINCT FROM OLD.payload AND NEW.payload IS NOT NULL)
        OR (OLD.compacted_at IS NOT NULL
            AND NEW.compacted_at IS DISTINCT FROM OLD.compacted_at)
    THEN
        RAISE EXCEPTION 'config_versions rows are immutable (only compaction is allowed)'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$;
"""
    )
    op.drop_constraint(op.f("ck_config_versions_catalog_state_compaction"), "config_versions")
    op.drop_column("config_versions", "catalog_state")
