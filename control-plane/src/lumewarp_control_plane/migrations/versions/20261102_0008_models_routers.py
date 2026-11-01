"""Rename routing resources without changing their relationships or wire format.

Revision ID: 0008
Revises: 0007

Tables/columns and their constraints/indexes are renamed in place. These tables use
UUID primary keys, so there are no routing-resource sequences to rename. Config
versions store binary protobuf payloads: unchanged field numbers preserve all
versions and their digests, including rollback and gateway snapshot.pb caches.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_TABLES = (
    ("deployments", "models"),
    ("routes", "routers"),
    ("route_deployments", "router_models"),
    ("route_fallbacks", "router_fallbacks"),
    ("route_splits", "router_splits"),
    ("route_split_deployments", "router_split_models"),
)
_COLUMNS = {
    "router_models": (("route_id", "router_id"), ("deployment_id", "model_id")),
    "router_fallbacks": (("route_id", "router_id"), ("fallback_route_id", "fallback_router_id")),
    "router_splits": (("route_id", "router_id"),),
    "router_split_models": (("deployment_id", "model_id"),),
    "usage_events": (("deployment", "model"),),
}


def _rename(*, reverse: bool) -> None:
    bind = op.get_bind()
    quote = bind.dialect.identifier_preparer.quote
    tables = tuple((new, old) if reverse else (old, new) for old, new in _TABLES)
    # PostgreSQL renames the backing index along with PK/UNIQUE constraints.
    inspector = sa.inspect(bind)
    constraints = []
    indexes = []
    for old, new in tables:
        for kind in (
            "get_pk_constraint",
            "get_unique_constraints",
            "get_foreign_keys",
            "get_check_constraints",
        ):
            found = getattr(inspector, kind)(old)
            if isinstance(found, dict):
                found = [found]
            for item in found:
                name = item.get("name")
                if name:
                    constraints.append((new, name))
        indexes.extend(
            item["name"]
            for item in inspector.get_indexes(old)
            if not item.get("duplicates_constraint")
        )

    def renamed(name: str) -> str:
        if reverse:
            return name.replace("router", "route").replace("model", "deployment")
        return name.replace("deployment", "model").replace("route", "router")

    for old, new in tables:
        op.rename_table(old, new)
    for table, columns in _COLUMNS.items():
        target = next((old for old, new in _TABLES if new == table), table) if reverse else table
        for old, new in columns:
            op.alter_column(
                target, new if reverse else old, new_column_name=old if reverse else new
            )
    for table, old in constraints:
        new = renamed(old)
        if new != old:
            op.execute(
                sa.text(
                    f"ALTER TABLE {quote(table)} RENAME CONSTRAINT {quote(old)} TO {quote(new)}"
                )
            )
    for old in indexes:
        new = renamed(old)
        if new != old:
            op.execute(sa.text(f"ALTER INDEX {quote(old)} RENAME TO {quote(new)}"))
    for old, new in (
        (("model", "deployment"), ("router", "route"))
        if reverse
        else (("deployment", "model"), ("route", "router"))
    ):
        bind.execute(
            sa.text(
                "UPDATE audit_log SET action = :new || substring(action FROM CAST(:start AS integer)) WHERE action LIKE :prefix"
            ),
            {"new": new, "start": len(old) + 1, "prefix": old + ".%"},
        )
        bind.execute(
            sa.text("UPDATE audit_log SET resource_type = :new WHERE resource_type = :old"),
            {"new": new, "old": old},
        )


def upgrade() -> None:
    _rename(reverse=False)


def downgrade() -> None:
    _rename(reverse=True)
