"""Archiving a team while a key create or rotate is in flight.

Both writers take the config scope row lock first, so they serialize: whichever runs second
sees the other's committed state. Archive revokes every non-revoked key of the team,
including one created or rotated just before it, so no live key can survive on an archived
team.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api import keys, teams
from lumewarp_control_plane.api.schemas import KeyCreate, KeyRotate
from lumewarp_control_plane.authz import RequestAuthz
from lumewarp_control_plane.models import ApiKey, KeyLifecycle, Team
from lumewarp_control_plane.security import Principal, PrincipalKind
from tests.factories import make_key, make_service_account, make_team


@pytest.mark.parametrize("operation", ["create", "rotate"])
async def test_archive_leaves_no_live_key_after_an_in_flight_write(
    client, committed, api_engine, api_settings, monkeypatch, operation
) -> None:
    team = await make_team(committed)
    account = await make_service_account(committed, team)
    key = await make_key(committed, team, account) if operation == "rotate" else None
    await committed.commit()

    key_scope_locked = asyncio.Event()
    key_row_ready = asyncio.Event()
    archive_scope_attempted = asyncio.Event()
    original_key_lock = keys.lock_scope
    original_team_lock = teams.lock_scope
    original_get_team = keys.get_team_or_404
    original_get_key = keys._get_key

    async def key_lock(session, settings):
        await original_key_lock(session, settings)
        key_scope_locked.set()

    async def get_team(session, authz, team_id):
        assert key_scope_locked.is_set(), "the scope lock comes before any row read"
        row = await original_get_team(session, authz, team_id)
        key_row_ready.set()
        await archive_scope_attempted.wait()
        return row

    async def get_key(session, authz, key_id):
        assert key_scope_locked.is_set(), "the scope lock comes before any row read"
        row = await original_get_key(session, authz, key_id)
        key_row_ready.set()
        await archive_scope_attempted.wait()
        return row

    async def team_lock(session, settings):
        await key_row_ready.wait()
        archive_scope_attempted.set()
        # Blocks on the scope row until the key transaction commits, then archives the key
        # that write just left behind.
        await original_team_lock(session, settings)

    monkeypatch.setattr(keys, "lock_scope", key_lock)
    monkeypatch.setattr(keys, "get_team_or_404", get_team)
    monkeypatch.setattr(keys, "_get_key", get_key)
    monkeypatch.setattr(teams, "lock_scope", team_lock)

    request = SimpleNamespace(app=client.app)

    def new_authz():
        return RequestAuthz(Principal(kind=PrincipalKind.BREAK_GLASS), client.app.state.authorizer)

    async with (
        AsyncSession(api_engine, expire_on_commit=False) as key_session,
        AsyncSession(api_engine, expire_on_commit=False) as team_session,
    ):
        if operation == "create":
            write = keys.create_key(
                team.id,
                KeyCreate(name="in-flight", service_account_id=account.id),
                request,
                new_authz(),
                key_session,
                api_settings,
                client.app.state.metrics,
            )
        else:
            assert key is not None
            write = keys.rotate_key(
                key.id,
                request,
                new_authz(),
                key_session,
                api_settings,
                client.app.state.metrics,
                KeyRotate(grace_seconds=3600),
            )
        async with asyncio.timeout(15), asyncio.TaskGroup() as tasks:
            tasks.create_task(write)
            tasks.create_task(teams.delete_team(team.id, new_authz(), team_session, api_settings))

    assert key_row_ready.is_set() and archive_scope_attempted.is_set()
    async with AsyncSession(api_engine) as check:
        archived = await check.get(Team, team.id)
        assert archived is not None and archived.deleted_at is not None
        live = (
            await check.scalars(
                select(ApiKey).where(
                    ApiKey.team_id == team.id, ApiKey.lifecycle != KeyLifecycle.REVOKED
                )
            )
        ).all()
        assert live == []
        rows = (await check.scalars(select(ApiKey).where(ApiKey.team_id == team.id))).all()
        assert rows and all(row.revoked_at is not None for row in rows)
        # The rotation-grace hash died with the key as well.
        assert all(row.previous_key_hash is None for row in rows)
