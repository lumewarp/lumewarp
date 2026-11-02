"""Rotation grace is compiled as a second hash with its own expiry."""

from datetime import UTC, datetime, timedelta

from lumewarp_control_plane.compiler import compile_scope
from lumewarp_control_plane.models import KeyLifecycle
from lumewarp_control_plane.security import hash_secret
from tests import factories
from tests.compiler.helpers import SCOPE, seed_scope


async def _compiled(factory, now: datetime | None = None):
    async with factory() as session:
        return await compile_scope(session, SCOPE, now=now, require_local_membership=True)


def _key(compiled, key_id: str):
    return next(k for k in compiled.snapshot.api_keys if k.key_id == key_id)


async def test_previous_hash_is_published_with_its_own_expiry(factory) -> None:
    await seed_scope(factory)
    now = datetime.now(UTC)
    old_hash = hash_secret("led-old-secret")
    async with factory() as session, session.begin():
        team = await factories.make_team(session)
        user = await factories.make_user(session)
        await factories.make_member(session, team, user)
        key = await factories.make_key(session, team, user)
        key.previous_key_hash = old_hash
        key.previous_key_expires_at = now + timedelta(seconds=60)
        key_id = str(key.id)

    compiled = await _compiled(factory, now=now)
    published = _key(compiled, key_id)
    assert published.previous_key_hash == old_hash
    assert published.previous_key_expires_at_unix_ms > 0
    assert "rotation_grace" in compiled.snapshot.required_capabilities

    # After its own deadline the compiler drops it; the delta then removes it everywhere.
    compiled = await _compiled(factory, now=now + timedelta(seconds=61))
    published = _key(compiled, key_id)
    assert published.previous_key_hash == b""
    assert published.previous_key_expires_at_unix_ms == 0
    assert "rotation_grace" not in compiled.snapshot.required_capabilities


async def test_previous_hash_never_outlives_the_key(factory) -> None:
    await seed_scope(factory)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        team = await factories.make_team(session)
        user = await factories.make_user(session)
        await factories.make_member(session, team, user)
        key = await factories.make_key(session, team, user, expires_at=now + timedelta(seconds=30))
        key.previous_key_hash = hash_secret("led-old-secret")
        key.previous_key_expires_at = now + timedelta(seconds=120)
        key_id = str(key.id)

    published = _key(await _compiled(factory, now=now), key_id)
    assert published.previous_key_expires_at_unix_ms <= int(
        (now + timedelta(seconds=30)).timestamp() * 1000
    )


async def test_revoked_key_drops_both_hashes(factory) -> None:
    await seed_scope(factory)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        team = await factories.make_team(session)
        user = await factories.make_user(session)
        await factories.make_member(session, team, user)
        key = await factories.make_key(session, team, user)
        key.previous_key_hash = hash_secret("led-old-secret")
        key.previous_key_expires_at = now + timedelta(seconds=60)
        key.lifecycle = KeyLifecycle.REVOKED
        key.revoked_at = now
        key_id = str(key.id)

    compiled = await _compiled(factory, now=now)
    assert all(k.key_id != key_id for k in compiled.snapshot.api_keys)
    assert "rotation_grace" not in compiled.snapshot.required_capabilities
