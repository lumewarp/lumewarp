"""Published config versions and routing rollback."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from lumewarp_control_plane import audit
from lumewarp_control_plane.api.common import (
    LimitDep,
    SessionDep,
    SettingsDep,
    conflict,
    mutation,
    parse_int_cursor,
    validation_error,
)
from lumewarp_control_plane.api.schemas import (
    ConfigVersionOut,
    Page,
    RollbackCreate,
    RollbackOut,
)
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.compiler.strategy import StrategyRejected
from lumewarp_control_plane.errors import not_found
from lumewarp_control_plane.models import ConfigVersion

router = APIRouter(tags=["config"])


def _out(row: ConfigVersion) -> ConfigVersionOut:
    return ConfigVersionOut(
        scope=row.scope,
        version=row.version,
        digest=row.digest.hex(),
        size_bytes=row.size_bytes,
        key_count=row.key_count,
        source_revision=row.source_revision,
        kind=row.kind.value,
        rollback_of_version=row.rollback_of_version,
        created_at=row.created_at,
        compacted_at=row.compacted_at,
    )


@router.get(
    "/config/versions",
    response_model=Page[ConfigVersionOut],
    operation_id="listConfigVersions",
)
async def list_config_versions(
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitDep = 50,
    cursor: str | None = None,
) -> Page[ConfigVersionOut]:
    await authz.require(Action.CONFIG_READ)
    stmt = (
        select(ConfigVersion)
        .where(ConfigVersion.scope == settings.scope)
        .order_by(ConfigVersion.version.desc())
    )
    if cursor is not None:
        stmt = stmt.where(ConfigVersion.version < parse_int_cursor(cursor))
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    next_cursor = str(rows[limit - 1].version) if len(rows) > limit else None
    return Page[ConfigVersionOut](
        items=[_out(row) for row in rows[:limit]], next_cursor=next_cursor
    )


@router.post("/config/rollback", response_model=RollbackOut, operation_id="rollbackConfig")
async def rollback_config(
    body: RollbackCreate,
    authz: Authz,
    session: SessionDep,
    settings: SettingsDep,
) -> RollbackOut:
    await authz.require(Action.CONFIG_ROLLBACK)
    from lumewarp_control_plane import compiler

    async with mutation(session):
        try:
            version = await compiler.rollback_routing(
                session,
                settings.scope,
                body.version,
                require_local_membership=authz.authorizer.manages_grants,
                force=body.force,
                principal=authz.principal,
            )
        except StrategyRejected as exc:
            raise validation_error(exc.field, exc.message) from exc
        except LookupError as exc:
            raise not_found("config version not found") from exc
        except compiler.CompileError as exc:
            raise conflict(f"rollback failed: {exc}") from exc
        audit.record(
            session,
            action="config.rollback",
            resource_type="config_version",
            resource_id=version,
            principal=authz.principal,
            details={"to_version": body.version, **({"force": True} if body.force else {})},
        )
    return RollbackOut(version=version)
