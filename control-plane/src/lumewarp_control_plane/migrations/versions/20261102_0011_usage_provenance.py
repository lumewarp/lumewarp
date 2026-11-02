"""Offline usage provenance migration, holding ACCESS EXCLUSIVE until commit.

Revision ID: 0011
Revises: 0010

This project has no production deployment. DDL, set-based backfill and ordinary
index builds run in ONE transaction under the table lock; this is not an online
or transaction-batched migration. Updates skip rows whose values do not change.
0007/0008 already renamed requested/model/endpoint. 0010 merged simple routes
into models; resolve their ids via requested without rewriting serving strings.
Unknown historical upstreams/attempts remain empty/zero, never inferred from the
current catalog. Identity uses started_at when present, otherwise completion ts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("model_id_ts", ["model_id", "ts"], None),
    ("team_model_id_ts", ["team_id", "model_id", "ts"], None),
    ("alias_id_ts", ["alias_id", "ts"], "alias_id IS NOT NULL"),
    ("router_id_ts", ["router_id", "ts"], "router_id IS NOT NULL"),
    ("model_ts", ["model", "ts"], None),
    ("upstream_model_ts", ["upstream_model", "ts"], None),
)


def _candidates(kind: str) -> str:
    # Constants only, never user input. History outlives deletion; a deleted
    # router still supplies its historic string but cannot supply a live id.
    return f"""
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
          SELECT n.id, h.old_name, coalesce(h.since, t.created_at, '-infinity'::timestamptz),
            h.renamed_at
          FROM history h LEFT JOIN catalog_names n ON n.id = h.name_id AND n.kind = h.kind
          LEFT JOIN {kind + "s"} t ON t.name_id = n.id
        )
    """


def upgrade() -> None:
    op.alter_column("usage_events", "model", type_=sa.String(400))
    for name, length in (
        ("alias", 400),
        ("router", 400),
        ("upstream", 200),
        ("upstream_model", 400),
    ):
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
    op.execute("""
        UPDATE usage_events SET model = ''
        WHERE outcome = 'rejected' AND model <> ''
    """)
    op.execute(
        _candidates("router")
        + """
        UPDATE usage_events e SET router = c.name, router_id = c.id
        FROM candidates c WHERE e.requested = c.name AND e.model <> ''
          AND e.outcome <> 'rejected'
          AND coalesce(e.started_at, e.ts) >= c.since
          AND coalesce(e.started_at, e.ts) < c.until
          AND (e.router, e.router_id) IS DISTINCT FROM (c.name, c.id)
    """
    )
    op.execute(
        _candidates("model")
        + """
        , resolved AS (
          SELECT e.id, coalesce(
            (SELECT c.id FROM candidates c WHERE c.name = e.model
              AND coalesce(e.started_at, e.ts) >= c.since
              AND coalesce(e.started_at, e.ts) < c.until ORDER BY c.until LIMIT 1),
            -- A merged route can predate the deployment it was repointed to.
            -- For this fallback only, omit the lower bound; the earliest end
            -- after event time disambiguates subsequent reuse of the same name.
            (SELECT c.id FROM candidates c WHERE c.name = e.requested
              AND coalesce(e.started_at, e.ts) < c.until ORDER BY c.until LIMIT 1)
          ) AS model_id FROM usage_events e
          WHERE e.model <> '' AND e.outcome <> 'rejected'
        )
        UPDATE usage_events e SET model_id = r.model_id FROM resolved r
        WHERE e.id = r.id AND e.model_id IS DISTINCT FROM r.model_id
    """
    )
    for suffix, columns, predicate in _INDEXES:
        kwargs = {"postgresql_where": sa.text(predicate)} if predicate else {}
        op.create_index(f"ix_usage_events_{suffix}", "usage_events", columns, **kwargs)


def downgrade() -> None:
    """Destructively drop provenance; re-upgrade cannot recover wire-only fields.

    Alembic commits this step before attempting the irreversible 0010 downgrade.
    Even a subsequent failure targeting 0009 leaves the DB at 0010 with alias,
    upstream names, fallback, attempts and ids lost. Back up before downgrading.
    """
    for suffix, _, _ in reversed(_INDEXES):
        op.drop_index(f"ix_usage_events_{suffix}", table_name="usage_events")
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
