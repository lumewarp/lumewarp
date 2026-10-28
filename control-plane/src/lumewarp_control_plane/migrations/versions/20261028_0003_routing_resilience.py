"""P2 routing: health checks, circuit breakers, retries, fallbacks, traffic splits.

Revision ID: 0003
Revises: 0002 (20261028_0002_oidc_sessions)

Downgrade is lossy: it drops every P2 routing setting and maps the `vllm_load` strategy
to `least_in_flight` (its documented fallback in the gateway).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STRATEGIES_P1 = ("weighted", "least_in_flight", "lowest_latency")
STRATEGIES_P2 = (*STRATEGIES_P1, "vllm_load")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # upstreams: health check, circuit breaker, metrics URL.
    op.add_column("upstreams", sa.Column("health_check_path", sa.String(400), nullable=True))
    for name in (
        "health_check_interval_ms",
        "health_check_timeout_ms",
        "health_check_unhealthy_threshold",
        "health_check_healthy_threshold",
    ):
        op.add_column("upstreams", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(
        "upstreams",
        sa.Column(
            "circuit_breaker_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column("upstreams", sa.Column("circuit_failure_threshold", sa.Integer(), nullable=True))
    op.add_column("upstreams", sa.Column("circuit_open_ms", sa.Integer(), nullable=True))
    op.add_column("upstreams", sa.Column("metrics_url", sa.Text(), nullable=True))
    op.create_check_constraint(
        "health_check_shape",
        "upstreams",
        "(health_check_interval_ms IS NULL) = (health_check_timeout_ms IS NULL)"
        " AND (health_check_interval_ms IS NOT NULL OR (health_check_path IS NULL"
        " AND health_check_unhealthy_threshold IS NULL"
        " AND health_check_healthy_threshold IS NULL))",
    )
    op.create_check_constraint(
        "health_check_bounds",
        "upstreams",
        "health_check_interval_ms IS NULL OR (health_check_interval_ms >= 1000"
        " AND health_check_timeout_ms > 0"
        " AND health_check_timeout_ms < health_check_interval_ms)",
    )
    op.create_check_constraint(
        "health_check_thresholds",
        "upstreams",
        "(health_check_unhealthy_threshold IS NULL OR health_check_unhealthy_threshold > 0)"
        " AND (health_check_healthy_threshold IS NULL OR health_check_healthy_threshold > 0)",
    )
    op.create_check_constraint(
        "circuit_breaker_shape",
        "upstreams",
        "circuit_breaker_enabled OR (circuit_failure_threshold IS NULL"
        " AND circuit_open_ms IS NULL)",
    )
    op.create_check_constraint(
        "circuit_breaker_positive",
        "upstreams",
        "(circuit_failure_threshold IS NULL OR circuit_failure_threshold > 0)"
        " AND (circuit_open_ms IS NULL OR circuit_open_ms > 0)",
    )

    # routes: strategy enum gains vllm_load; retry policy; sticky.
    op.drop_constraint(op.f("ck_routes_routing_strategy"), "routes", type_="check")
    op.create_check_constraint("routing_strategy", "routes", _in("strategy", STRATEGIES_P2))
    op.add_column(
        "routes",
        sa.Column("retry_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("routes", sa.Column("retry_max_attempts", sa.SmallInteger(), nullable=True))
    op.add_column(
        "routes",
        sa.Column(
            "retry_on_status",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "routes",
        sa.Column("retry_total_timeout_ms", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "routes",
        sa.Column(
            "sticky",
            sa.Enum(
                "none",
                "header",
                "team",
                "key",
                name="sticky_by",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="none",
            nullable=False,
        ),
    )
    op.add_column("routes", sa.Column("sticky_header", sa.String(200), nullable=True))
    op.create_check_constraint(
        "retry_max_attempts_range",
        "routes",
        "retry_max_attempts IS NULL OR retry_max_attempts BETWEEN 1 AND 5",
    )
    op.create_check_constraint(
        "retry_status_range",
        "routes",
        "400 <= ALL (retry_on_status) AND 599 >= ALL (retry_on_status)",
    )
    op.create_check_constraint("retry_timeout_nonneg", "routes", "retry_total_timeout_ms >= 0")
    op.create_check_constraint(
        "retry_shape",
        "routes",
        "retry_enabled OR (retry_max_attempts IS NULL"
        " AND cardinality(retry_on_status) = 0 AND retry_total_timeout_ms = 0)",
    )
    op.create_check_constraint(
        "sticky_header_iff", "routes", "(sticky = 'header') = (sticky_header IS NOT NULL)"
    )

    op.create_table(
        "route_fallbacks",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("fallback_route_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "route_id <> fallback_route_id", name=op.f("ck_route_fallbacks_no_self")
        ),
        sa.ForeignKeyConstraint(
            ["fallback_route_id"],
            ["routes.id"],
            name=op.f("fk_route_fallbacks_fallback_route_id_routes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_route_fallbacks_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("route_id", "fallback_route_id", name=op.f("pk_route_fallbacks")),
        sa.UniqueConstraint(
            "route_id", "position", name=op.f("uq_route_fallbacks_route_id_position")
        ),
    )
    op.create_index(
        op.f("ix_route_fallbacks_fallback_route_id"), "route_fallbacks", ["fallback_route_id"]
    )
    op.create_table(
        "route_splits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9_.-]{1,32}$'", name=op.f("ck_route_splits_name_charset")
        ),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_route_splits_weight_nonneg")),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_route_splits_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_splits")),
        sa.UniqueConstraint("route_id", "name", name=op.f("uq_route_splits_route_id_name")),
        sa.UniqueConstraint("route_id", "position", name=op.f("uq_route_splits_route_id_position")),
    )
    op.create_table(
        "route_split_deployments",
        sa.Column("split_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_route_split_deployments_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["split_id"],
            ["route_splits.id"],
            name=op.f("fk_route_split_deployments_split_id_route_splits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "split_id", "deployment_id", name=op.f("pk_route_split_deployments")
        ),
        sa.UniqueConstraint(
            "split_id", "position", name=op.f("uq_route_split_deployments_split_id_position")
        ),
    )
    op.create_index(
        op.f("ix_route_split_deployments_deployment_id"),
        "route_split_deployments",
        ["deployment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_route_split_deployments_deployment_id"), table_name="route_split_deployments"
    )
    op.drop_table("route_split_deployments")
    op.drop_table("route_splits")
    op.drop_index(op.f("ix_route_fallbacks_fallback_route_id"), table_name="route_fallbacks")
    op.drop_table("route_fallbacks")

    for name in (
        "sticky_header_iff",
        "retry_shape",
        "retry_timeout_nonneg",
        "retry_status_range",
        "retry_max_attempts_range",
        "sticky_by",
    ):
        op.drop_constraint(op.f(f"ck_routes_{name}"), "routes", type_="check")
    for name in (
        "sticky_header",
        "sticky",
        "retry_total_timeout_ms",
        "retry_on_status",
        "retry_max_attempts",
        "retry_enabled",
    ):
        op.drop_column("routes", name)
    op.drop_constraint(op.f("ck_routes_routing_strategy"), "routes", type_="check")
    op.execute("UPDATE routes SET strategy = 'least_in_flight' WHERE strategy = 'vllm_load'")
    op.create_check_constraint("routing_strategy", "routes", _in("strategy", STRATEGIES_P1))

    for name in (
        "circuit_breaker_positive",
        "circuit_breaker_shape",
        "health_check_thresholds",
        "health_check_bounds",
        "health_check_shape",
    ):
        op.drop_constraint(op.f(f"ck_upstreams_{name}"), "upstreams", type_="check")
    for name in (
        "metrics_url",
        "circuit_open_ms",
        "circuit_failure_threshold",
        "circuit_breaker_enabled",
        "health_check_healthy_threshold",
        "health_check_unhealthy_threshold",
        "health_check_timeout_ms",
        "health_check_interval_ms",
        "health_check_path",
    ):
        op.drop_column("upstreams", name)
