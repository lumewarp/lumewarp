"""Convert routing to the catalog: names, models, aliases, routers, grants.

Revision ID: 0010
Revises: 0009

Conversion rules:
- A route with one member and no retry, fallback or split merges into a model named after
  the route; two such routes sharing a deployment each get their own model. A deployment
  still used elsewhere (or by nothing) stays a model under its own name.
- Remaining routes become routers; each member's weight is the old model weight. A route
  with a traffic split keeps only its split members, the only ones the gateway served.
- A route used as a fallback becomes its single member's model.
- `allowed_models` becomes `policy_grants`. A name that resolves to nothing is dropped and
  its count logged; there is nothing to convert it to.

The migration fails, changing nothing, and lists every problem when a remaining route and
a model would share a name, when two models would, or when a route with no single member
is used as a fallback.

Old `config_versions` are cleared by compacting them: their payloads and sidecars go, the
rows stay so version numbers keep increasing, and a recompile is queued for each scope.

Downgrade raises: this conversion is irreversible (merged routes, per-member weights and
id-based grants have no equivalent in the old tables).
"""

import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

_POLICY_TABLES = ("teams", "team_members", "service_accounts")
_MODEL_FIELDS = (
    "upstream_id",
    "upstream_model",
    "price_input_nano_usd_per_mtok",
    "price_output_nano_usd_per_mtok",
    "created_at",
    "updated_at",
)
_ROUTER_FIELDS = (
    "retry_enabled",
    "retry_max_attempts",
    "retry_on_status",
    "retry_total_timeout_ms",
    "sticky",
    "sticky_header",
    "created_at",
    "updated_at",
)


@dataclass
class _Conversion:
    names: list[dict[str, Any]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    routers: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    splits: list[dict[str, Any]] = field(default_factory=list)
    split_members: list[dict[str, Any]] = field(default_factory=list)


def _rows(bind: sa.Connection, sql: str) -> list[Any]:
    return list(bind.execute(sa.text(sql)).mappings().all())


def _json(value: Any) -> dict[str, Any]:
    """Raw SQL hands JSONB back as text on asyncpg."""
    if isinstance(value, str):
        value = json.loads(value)
    return value or {}


def _convert(bind: sa.Connection) -> _Conversion:
    deployments = _rows(bind, "SELECT * FROM models ORDER BY name")
    routes = _rows(bind, "SELECT * FROM routers ORDER BY name")
    weight = {d["id"]: d["weight"] for d in deployments}

    route_members: dict[Any, list[Any]] = defaultdict(list)
    for row in _rows(bind, "SELECT * FROM router_models ORDER BY router_id, position"):
        route_members[row["router_id"]].append(row["model_id"])
    route_fallbacks: dict[Any, list[Any]] = defaultdict(list)
    for row in _rows(bind, "SELECT * FROM router_fallbacks ORDER BY router_id, position"):
        route_fallbacks[row["router_id"]].append(row["fallback_router_id"])
    route_splits: dict[Any, list[Any]] = defaultdict(list)
    for row in _rows(bind, "SELECT * FROM router_splits ORDER BY router_id, position"):
        route_splits[row["router_id"]].append(row)
    split_models: dict[Any, list[Any]] = defaultdict(list)
    for row in _rows(bind, "SELECT * FROM router_split_models ORDER BY split_id, position"):
        split_models[row["split_id"]].append(row["model_id"])

    def merges(route: Any) -> bool:
        return (
            len(route_members[route["id"]]) == 1
            and not route["retry_enabled"]
            and not route_fallbacks[route["id"]]
            and not route_splits[route["id"]]
        )

    merging = [r for r in routes if merges(r)]
    remaining = [r for r in routes if not merges(r)]
    by_deployment: dict[Any, list[Any]] = defaultdict(list)
    for route in merging:
        if route["strategy"] == "custom":
            logger.warning(
                "router %s merges into a model: its custom strategy has no effect on one member",
                route["name"],
            )
        by_deployment[route_members[route["id"]][0]].append(route)
    used_elsewhere = {
        model_id
        for route in remaining
        for model_id in (
            *route_members[route["id"]],
            *(m for s in route_splits[route["id"]] for m in split_models[s["id"]]),
        )
    }

    out = _Conversion()
    merged: dict[Any, Any] = {}

    def add_model(model_id: Any, name: str, deployment: Any) -> None:
        out.names.append({"id": model_id, "name": name, "kind": "model"})
        out.models.append({"name_id": model_id, **{f: deployment[f] for f in _MODEL_FIELDS}})

    for deployment in deployments:
        own = by_deployment[deployment["id"]]
        same_name = next((r for r in own if r["name"] == deployment["name"]), None)
        rest = [r for r in own if r is not same_name]
        if same_name is not None or deployment["id"] in used_elsewhere or not own:
            add_model(deployment["id"], deployment["name"], deployment)
            if same_name is not None:
                merged[same_name["id"]] = deployment["id"]
        else:
            first = rest.pop(0)
            add_model(deployment["id"], first["name"], deployment)
            merged[first["id"]] = deployment["id"]
        for route in rest:
            add_model(route["id"], route["name"], deployment)
            merged[route["id"]] = route["id"]

    problems: list[str] = []
    model_names = [n["name"] for n in out.names]
    problems.extend(
        f"two models would be named {name!r}"
        for name in sorted({n for n in model_names if model_names.count(n) > 1})
    )
    problems.extend(
        f"router and model both named {r['name']!r}" for r in remaining if r["name"] in model_names
    )

    for route in remaining:
        router_id = route["id"]
        out.names.append({"id": router_id, "name": route["name"], "kind": "router"})
        custom = route["strategy"] == "custom"
        out.routers.append(
            {
                "name_id": router_id,
                "strategy": route["custom_strategy"] if custom else route["strategy"],
                "params": _json(route["strategy_params"]),
                **{f: route[f] for f in _ROUTER_FIELDS},
            }
        )
        if not route_splits[router_id]:
            out.members.extend(
                {
                    "router_id": router_id,
                    "model_id": model_id,
                    "weight": weight[model_id],
                    "position": position,
                }
                for position, model_id in enumerate(route_members[router_id])
            )
        for split in route_splits[router_id]:
            out.splits.append(
                {k: split[k] for k in ("id", "router_id", "name", "weight", "position")}
            )
            out.split_members.extend(
                {
                    "split_id": split["id"],
                    "model_id": model_id,
                    "weight": weight[model_id],
                    "position": position,
                }
                for position, model_id in enumerate(split_models[split["id"]])
            )

    names = {r["id"]: r["name"] for r in routes}
    by_id = {r["id"]: r for r in routes}
    for route in remaining:
        targets: list[Any] = []
        for fallback in route_fallbacks[route["id"]]:
            members = route_members[fallback]
            if merges(by_id[fallback]):
                target = merged[fallback]
            elif len(members) == 1 and not route_splits[fallback]:
                target = members[0]
            else:
                problems.append(
                    f"router {route['name']!r} falls back to {names[fallback]!r},"
                    " which has no single member"
                )
                continue
            if target not in targets:
                targets.append(target)
        out.fallbacks.extend(
            {"router_id": route["id"], "model_id": target, "position": position}
            for position, target in enumerate(targets)
        )

    if problems:
        raise RuntimeError("catalog conversion failed: " + "; ".join(sorted(problems)))
    return out


def _convert_grants(bind: sa.Connection, ids: dict[str, Any]) -> list[dict[str, Any]]:
    grants: list[dict[str, Any]] = []
    dropped = 0
    owners = (
        (
            "SELECT id AS team_id, NULL::uuid AS user_id, NULL::uuid AS service_account_id,"
            " allowed_models FROM teams"
        ),
        (
            "SELECT team_id, user_id, NULL::uuid AS service_account_id, allowed_models"
            " FROM team_members"
        ),
        (
            "SELECT team_id, NULL::uuid AS user_id, id AS service_account_id, allowed_models"
            " FROM service_accounts"
        ),
    )
    for sql in owners:
        for owner in _rows(bind, sql):
            for name in dict.fromkeys(owner["allowed_models"]):
                name_id = ids.get(name)
                if name_id is None:
                    dropped += 1
                    continue
                grants.append(
                    {
                        "id": uuid.uuid4(),
                        "name_id": name_id,
                        "team_id": owner["team_id"],
                        "user_id": owner["user_id"],
                        "service_account_id": owner["service_account_id"],
                    }
                )
    if dropped:
        logger.warning("catalog conversion dropped %d grants of unknown names", dropped)
    return grants


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _name_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["name_id", "kind"],
        ["catalog_names.id", "catalog_names.kind"],
        name=op.f(f"fk_{table}_name_id_kind_catalog_names"),
        ondelete="CASCADE",
    )


def _member_columns() -> list[Any]:
    return [
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    converted = _convert(bind)
    grants = _convert_grants(bind, {n["name"]: n["id"] for n in converted.names})

    for table in ("router_split_models", "router_splits", "router_fallbacks", "router_models"):
        op.drop_table(table)
    op.drop_table("routers")
    op.drop_table("models")
    for table in _POLICY_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_allowlist_mode"), table)
        op.drop_column(table, "allowed_models")

    names = op.create_table(
        "catalog_names",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(400), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "kind IN ('model', 'alias', 'router')", name=op.f("ck_catalog_names_catalog_kind")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_names")),
        sa.UniqueConstraint("id", "kind", name=op.f("uq_catalog_names_id_kind")),
        sa.UniqueConstraint("name", name=op.f("uq_catalog_names_name")),
    )
    op.create_table(
        "catalog_name_history",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("old_name", sa.String(400), nullable=False),
        sa.Column(
            "renamed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('model', 'alias', 'router')",
            name=op.f("ck_catalog_name_history_catalog_kind"),
        ),
        sa.PrimaryKeyConstraint(
            "name_id", "old_name", "renamed_at", name=op.f("pk_catalog_name_history")
        ),
    )
    op.create_index(
        op.f("ix_catalog_name_history_old_name"), "catalog_name_history", ["old_name", "kind"]
    )
    models = op.create_table(
        "models",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), server_default="model", nullable=False),
        sa.Column("upstream_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_model", sa.String(400), nullable=False),
        sa.Column("price_input_nano_usd_per_mtok", sa.BigInteger(), nullable=True),
        sa.Column("price_output_nano_usd_per_mtok", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("kind = 'model'", name=op.f("ck_models_kind_model")),
        sa.CheckConstraint(
            "(price_input_nano_usd_per_mtok IS NULL) = (price_output_nano_usd_per_mtok IS NULL)",
            name=op.f("ck_models_price_both_or_neither"),
        ),
        sa.CheckConstraint(
            "price_input_nano_usd_per_mtok IS NULL OR (price_input_nano_usd_per_mtok >= 0"
            " AND price_output_nano_usd_per_mtok >= 0)",
            name=op.f("ck_models_price_nonneg"),
        ),
        _name_fk("models"),
        sa.ForeignKeyConstraint(
            ["upstream_id"],
            ["upstreams.id"],
            name=op.f("fk_models_upstream_id_upstreams"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("name_id", name=op.f("pk_models")),
    )
    op.create_index(op.f("ix_models_upstream_id"), "models", ["upstream_id"])
    op.create_table(
        "aliases",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), server_default="alias", nullable=False),
        sa.Column("target_name_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("kind = 'alias'", name=op.f("ck_aliases_kind_alias")),
        sa.CheckConstraint(
            "target_kind IN ('model', 'router')", name=op.f("ck_aliases_target_not_alias")
        ),
        _name_fk("aliases"),
        sa.ForeignKeyConstraint(
            ["target_name_id", "target_kind"],
            ["catalog_names.id", "catalog_names.kind"],
            name=op.f("fk_aliases_target_name_id_target_kind_catalog_names"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("name_id", name=op.f("pk_aliases")),
    )
    op.create_index(op.f("ix_aliases_target_name_id"), "aliases", ["target_name_id"])
    routers = op.create_table(
        "routers",
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), server_default="router", nullable=False),
        sa.Column("strategy", sa.String(63), server_default="weighted", nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("retry_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("retry_max_attempts", sa.SmallInteger(), nullable=True),
        sa.Column(
            "retry_on_status",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("retry_total_timeout_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sticky", sa.String(32), server_default="none", nullable=False),
        sa.Column("sticky_header", sa.String(200), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("kind = 'router'", name=op.f("ck_routers_kind_router")),
        sa.CheckConstraint(
            "strategy ~ '^[a-z][a-z0-9_]{0,62}$'", name=op.f("ck_routers_strategy_name")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_routers_params_object")
        ),
        sa.CheckConstraint(
            "retry_max_attempts IS NULL OR retry_max_attempts BETWEEN 1 AND 5",
            name=op.f("ck_routers_retry_max_attempts_range"),
        ),
        sa.CheckConstraint(
            "400 <= ALL (retry_on_status) AND 599 >= ALL (retry_on_status)",
            name=op.f("ck_routers_retry_status_range"),
        ),
        sa.CheckConstraint(
            "retry_total_timeout_ms >= 0", name=op.f("ck_routers_retry_timeout_nonneg")
        ),
        sa.CheckConstraint(
            "retry_enabled OR (retry_max_attempts IS NULL"
            " AND cardinality(retry_on_status) = 0 AND retry_total_timeout_ms = 0)",
            name=op.f("ck_routers_retry_shape"),
        ),
        sa.CheckConstraint(
            "(sticky = 'header') = (sticky_header IS NOT NULL)",
            name=op.f("ck_routers_sticky_header_iff"),
        ),
        sa.CheckConstraint(
            "sticky IN ('none', 'header', 'team', 'key')", name=op.f("ck_routers_sticky_by")
        ),
        _name_fk("routers"),
        sa.PrimaryKeyConstraint("name_id", name=op.f("pk_routers")),
    )
    members = op.create_table(
        "router_members",
        sa.Column("router_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        *_member_columns(),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_router_members_weight_nonneg")),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_router_members_params_object")
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.name_id"],
            name=op.f("fk_router_members_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["routers.name_id"],
            name=op.f("fk_router_members_router_id_routers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("router_id", "model_id", name=op.f("pk_router_members")),
        sa.UniqueConstraint(
            "router_id", "position", name=op.f("uq_router_members_router_id_position")
        ),
    )
    op.create_index(op.f("ix_router_members_model_id"), "router_members", ["model_id"])
    fallbacks = op.create_table(
        "router_fallbacks",
        sa.Column("router_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.name_id"],
            name=op.f("fk_router_fallbacks_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["routers.name_id"],
            name=op.f("fk_router_fallbacks_router_id_routers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("router_id", "model_id", name=op.f("pk_router_fallbacks")),
        sa.UniqueConstraint(
            "router_id", "position", name=op.f("uq_router_fallbacks_router_id_position")
        ),
    )
    op.create_index(op.f("ix_router_fallbacks_model_id"), "router_fallbacks", ["model_id"])
    splits = op.create_table(
        "router_splits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("router_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9_.-]{1,32}$'", name=op.f("ck_router_splits_name_charset")
        ),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_router_splits_weight_nonneg")),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["routers.name_id"],
            name=op.f("fk_router_splits_router_id_routers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_router_splits")),
        sa.UniqueConstraint("router_id", "name", name=op.f("uq_router_splits_router_id_name")),
        sa.UniqueConstraint(
            "router_id", "position", name=op.f("uq_router_splits_router_id_position")
        ),
    )
    split_members = op.create_table(
        "router_split_members",
        sa.Column("split_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        *_member_columns(),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_router_split_members_weight_nonneg")),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_router_split_members_params_object")
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.name_id"],
            name=op.f("fk_router_split_members_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["split_id"],
            ["router_splits.id"],
            name=op.f("fk_router_split_members_split_id_router_splits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("split_id", "model_id", name=op.f("pk_router_split_members")),
        sa.UniqueConstraint(
            "split_id", "position", name=op.f("uq_router_split_members_split_id_position")
        ),
    )
    op.create_index(op.f("ix_router_split_members_model_id"), "router_split_members", ["model_id"])
    policy_grants = op.create_table(
        "policy_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("service_account_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "user_id IS NULL OR service_account_id IS NULL", name=op.f("ck_policy_grants_one_owner")
        ),
        sa.ForeignKeyConstraint(
            ["name_id"],
            ["catalog_names.id"],
            name=op.f("fk_policy_grants_name_id_catalog_names"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_policy_grants_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "user_id"],
            ["team_members.team_id", "team_members.user_id"],
            name=op.f("fk_policy_grants_team_id_user_id_team_members"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_account_id", "team_id"],
            ["service_accounts.id", "service_accounts.team_id"],
            name=op.f("fk_policy_grants_service_account_id_team_id_service_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_grants")),
    )
    op.create_index(op.f("ix_policy_grants_name_id"), "policy_grants", ["name_id"])
    op.create_index(
        "uq_policy_grants_team",
        "policy_grants",
        ["team_id", "name_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL AND service_account_id IS NULL"),
    )
    op.create_index(
        "uq_policy_grants_member",
        "policy_grants",
        ["team_id", "user_id", "name_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_policy_grants_service_account",
        "policy_grants",
        ["service_account_id", "name_id"],
        unique=True,
        postgresql_where=sa.text("service_account_id IS NOT NULL"),
    )

    for table, rows in (
        (names, converted.names),
        (models, [{**r, "kind": "model"} for r in converted.models]),
        (routers, [{**r, "kind": "router"} for r in converted.routers]),
        (members, [{**r, "params": {}} for r in converted.members]),
        (fallbacks, converted.fallbacks),
        (splits, converted.splits),
        (split_members, [{**r, "params": {}} for r in converted.split_members]),
        (policy_grants, grants),
    ):
        if rows:
            op.bulk_insert(table, rows)

    # Compaction is the one change the immutability trigger allows. Publishing resumes with
    # the next number once the queued recompile runs.
    op.execute(
        "UPDATE config_versions SET payload = NULL, catalog_state = NULL, compacted_at = now()"
        " WHERE payload IS NOT NULL"
    )
    op.execute(
        """
WITH bumped AS (
    UPDATE scopes SET source_revision = source_revision + 1
    RETURNING name, source_revision
)
INSERT INTO config_outbox (scope, reason, source_revision)
SELECT name, 'migration.catalog', source_revision FROM bumped
"""
    )


def downgrade() -> None:
    raise NotImplementedError("0010 is irreversible: restore a backup to go back to 0009")
