"""Store usage provenance and catalog identities; backfill in bounded id batches.

Revision ID: 0011
Revises: 0010

0007/0008 already renamed requested/model/endpoint. 0010 merged simple routes
into models; resolve those legacy serving ids via requested where necessary,
without rewriting the recorded serving-name strings.
Upstream provenance cannot be reconstructed reliably, so old rows retain empty
strings rather than inventing an upstream from today's catalog.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIELDS = ("model_id", "alias_id", "router_id", "model", "alias", "router", "upstream_model")


def upgrade() -> None:
    op.alter_column("usage_events", "model", type_=sa.String(400))
    for name, length in (
        ("alias", 400),
        ("router", 400),
        ("upstream", 200),
        ("upstream_model", 400),
    ):
        # PostgreSQL constant defaults add columns without rewriting the table.
        op.add_column(
            "usage_events", sa.Column(name, sa.String(length), nullable=False, server_default="")
        )
    op.add_column(
        "usage_events",
        sa.Column("fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "usage_events", sa.Column("attempts", sa.BigInteger(), nullable=False, server_default="0")
    )
    for kind in ("model", "alias", "router"):
        op.add_column("usage_events", sa.Column(f"{kind}_id", postgresql.UUID(), nullable=True))
    op.create_check_constraint("attempts_nonneg", "usage_events", "attempts >= 0")
    bind = op.get_bind()
    after = 0
    while True:
        ids = list(
            bind.execute(
                sa.text("SELECT id FROM usage_events WHERE id > :after ORDER BY id LIMIT 10000"),
                {"after": after},
            ).scalars()
        )
        if not ids:
            break
        high = ids[-1]
        bind.execute(
            sa.text("""
            UPDATE usage_events e SET
              model = CASE WHEN e.outcome = 'rejected' THEN '' ELSE e.model END,
              router = CASE WHEN called.kind = 'router' AND e.model <> ''
                AND e.outcome <> 'rejected' THEN called.name ELSE '' END
            FROM (SELECT id, requested FROM usage_events WHERE id > :after AND id <= :high) batch
            LEFT JOIN catalog_names called ON called.name = batch.requested
            WHERE e.id = batch.id
        """),
            {"after": after, "high": high},
        )
        # Resolve each kind separately. Historical intervals use the preceding
        # rename, not simply the most recent matching spelling.
        for kind in ("model", "router"):
            match = f"e.{kind} = c.name"
            if kind == "model":
                # A simple legacy route may have absorbed its deployment. Keep
                # the old deployment spelling, but attach the merged model id.
                match = """(e.model = c.name OR (e.requested = c.name AND NOT EXISTS (
                    SELECT 1 FROM candidates original WHERE original.name = e.model
                      AND e.ts >= original.since AND e.ts < original.until
                )))"""
            bind.execute(
                sa.text(f"""
                WITH history AS (
                  SELECT h.*, lag(renamed_at) OVER
                    (PARTITION BY name_id ORDER BY renamed_at) AS since
                  FROM catalog_name_history h WHERE kind = '{kind}'
                ), candidates AS (
                  SELECT n.id, n.name, greatest(t.created_at,
                    (SELECT max(renamed_at) FROM history WHERE name_id = n.id)) AS since,
                    'infinity'::timestamptz AS until
                  FROM catalog_names n JOIN {kind + "s"} t ON t.name_id = n.id
                  WHERE n.kind = '{kind}'
                  UNION ALL
                  SELECT h.name_id, h.old_name, coalesce(h.since, t.created_at), h.renamed_at
                  FROM history h JOIN {kind + "s"} t ON t.name_id = h.name_id
                )
                UPDATE usage_events e SET {kind}_id = c.id
                FROM candidates c WHERE e.id > :after AND e.id <= :high
                  AND {match} AND e.{kind} <> '' AND e.model <> ''
                  AND e.ts >= c.since AND e.ts < c.until
            """),
                {"after": after, "high": high},
            )
        after = high
    for field in _FIELDS:
        op.create_index(f"ix_usage_events_{field}_ts", "usage_events", [field, "ts"])


def downgrade() -> None:
    for field in reversed(_FIELDS):
        op.drop_index(f"ix_usage_events_{field}_ts", table_name="usage_events")
    op.drop_constraint(op.f("ck_usage_events_attempts_nonneg"), "usage_events", type_="check")
    for name in (
        "model_id",
        "alias_id",
        "router_id",
        "alias",
        "router",
        "upstream",
        "upstream_model",
        "fallback",
        "attempts",
    ):
        op.drop_column("usage_events", name)
    # Do not truncate model names accepted since the upgrade.
