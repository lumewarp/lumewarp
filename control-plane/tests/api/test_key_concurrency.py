"""Key writers and owner removal share scope-before-row transaction lock order."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.api import keys, members
from lumewarp_control_plane.api.schemas import KeyUpdate
from lumewarp_control_plane.authz import RequestAuthz
from lumewarp_control_plane.models import ApiKey, KeyLifecycle, TeamMember
from lumewarp_control_plane.security import Principal, PrincipalKind
from tests.factories import make_key, make_member, make_team, make_user


@pytest.mark.parametrize("operation", ["update", "rotate", "revoke"])
async def test_key_mutation_and_member_removal_do_not_deadlock(
    client, committed, api_engine, api_settings, monkeypatch, operation
) -> None:
    team = await make_team(committed)
    owner = await make_user(committed)
    await make_member(committed, team, owner)
    key = await make_key(committed, team, owner)
    await committed.commit()
    key_scope_locked = asyncio.Event()
    key_locked = asyncio.Event()
    removal_scope_attempted = asyncio.Event()
    original_key_scope = keys.lock_scope
    original_get_key = keys._get_key
    original_member_scope = members.lock_scope

    async def key_scope(session, settings):
        await original_key_scope(session, settings)
        key_scope_locked.set()

    async def get_key(session, authz, key_id):
        assert key_scope_locked.is_set(), "key row must never be locked before its scope"
        row = await original_get_key(session, authz, key_id)
        key_locked.set()
        await removal_scope_attempted.wait()
        return row

    async def member_scope(session, settings):
        await key_locked.wait()
        removal_scope_attempted.set()
        await original_member_scope(session, settings)

    monkeypatch.setattr(keys, "lock_scope", key_scope)
    monkeypatch.setattr(keys, "_get_key", get_key)
    monkeypatch.setattr(members, "lock_scope", member_scope)
    authz = RequestAuthz(Principal(kind=PrincipalKind.BREAK_GLASS), client.app.state.authorizer)
    request = SimpleNamespace(app=client.app)
    async with (
        AsyncSession(api_engine, expire_on_commit=False) as key_session,
        AsyncSession(api_engine, expire_on_commit=False) as member_session,
    ):
        if operation == "update":
            mutation = keys.update_key(
                key.id,
                KeyUpdate(name="renamed"),
                request,
                authz,
                key_session,
                api_settings,
                client.app.state.metrics,
            )
        elif operation == "rotate":
            mutation = keys.rotate_key(
                key.id,
                request,
                authz,
                key_session,
                api_settings,
                client.app.state.metrics,
            )
        else:
            mutation = keys.revoke_key(key.id, authz, key_session, api_settings)
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            tasks.create_task(mutation)
            tasks.create_task(
                members.delete_member(
                    team.id,
                    owner.id,
                    request,
                    authz,
                    member_session,
                    api_settings,
                    client.app.state.metrics,
                )
            )
    assert key_locked.is_set() and removal_scope_attempted.is_set()
    async with AsyncSession(api_engine) as check:
        assert await check.get(TeamMember, (team.id, owner.id)) is None
        stored = await check.get(ApiKey, key.id)
        if operation == "revoke":
            assert stored.lifecycle is KeyLifecycle.REVOKED
            assert stored.suspended_reason is None
        else:
            assert stored.lifecycle is KeyLifecycle.SUSPENDED
            assert stored.suspended_reason == "owner_removed"
