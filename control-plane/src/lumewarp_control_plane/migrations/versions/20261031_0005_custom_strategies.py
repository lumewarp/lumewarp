"""Compiled-in custom routing strategies stored on routes.

Revision ID: 0005
Revises: 0004
Downgrade drops custom settings and maps custom routes to least_in_flight,
which also accepts deployments with zero weight.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

BUILTINS = ("weighted", "least_in_flight", "lowest_latency", "vllm_load")


def _strategies(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_routes_routing_strategy"), "routes", type_="check")
    op.create_check_constraint(
        "routing_strategy", "routes", f"strategy IN ({', '.join(repr(v) for v in values)})"
    )


def upgrade() -> None:
    _strategies((*BUILTINS, "custom"))
    op.add_column("routes", sa.Column("custom_strategy", sa.String(63), nullable=True))
    op.add_column(
        "routes", sa.Column("strategy_params", postgresql.JSONB(none_as_null=True), nullable=True)
    )
    op.create_check_constraint(
        "custom_strategy_shape",
        "routes",
        "(strategy = 'custom') = (custom_strategy IS NOT NULL)"
        " AND (strategy = 'custom' OR strategy_params IS NULL)",
    )
    op.create_check_constraint(
        "custom_strategy_name",
        "routes",
        "custom_strategy IS NULL OR custom_strategy ~ '^[a-z][a-z0-9_]{0,62}$'",
    )
    op.create_check_constraint(
        "strategy_params_object",
        "routes",
        "strategy_params IS NULL OR jsonb_typeof(strategy_params) = 'object'",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE routes SET strategy = 'least_in_flight', custom_strategy = NULL,"
        " strategy_params = NULL WHERE strategy = 'custom'"
    )
    for name in ("custom_strategy_shape", "custom_strategy_name", "strategy_params_object"):
        op.drop_constraint(op.f(f"ck_routes_{name}"), "routes", type_="check")
    op.drop_column("routes", "strategy_params")
    op.drop_column("routes", "custom_strategy")
    _strategies(BUILTINS)
