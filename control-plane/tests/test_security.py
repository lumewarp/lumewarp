import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.db import get_session
from lumewarp_control_plane.models import PersonalAccessToken, User
from lumewarp_control_plane.security import (
    CurrentPrincipal,
    PrincipalKind,
    SecretKind,
    generate_secret,
    hash_secret,
    parse_bearer,
    secret_kind,
)
from lumewarp_control_plane.settings import Settings
from tests import factories


@pytest.mark.parametrize("kind", list(SecretKind))
def test_generate_secret(kind: SecretKind) -> None:
    a, b = generate_secret(kind), generate_secret(kind)
    assert a.secret.startswith(kind.value)
    assert len(a.secret) == len(kind.value) + 43
    assert a.secret != b.secret
    assert a.hash == hash_secret(a.secret) and len(a.hash) == 32
    assert a.secret.startswith(a.display_prefix) and len(a.display_prefix) < len(a.secret)
    assert secret_kind(a.secret) is kind


def test_hash_is_plain_sha256() -> None:
    # The gateway hashes presented keys the same way (snapshot ApiKey.key_hash).
    assert hash_secret("lw-abc") == hashlib.sha256(b"lw-abc").digest()


@pytest.mark.parametrize(
    ("header", "token"),
    [
        ("Bearer abc", "abc"),
        ("bearer   abc ", "abc"),
        (None, None),
        ("", None),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer a b", None),
    ],
)
def test_parse_bearer(header: str | None, token: str | None) -> None:
    assert parse_bearer(header) == token


def test_secret_kind_unknown() -> None:
    assert secret_kind("sk-123") is None


def _whoami_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/whoami")
    async def whoami(p: CurrentPrincipal) -> dict:
        return {
            "kind": p.kind.value,
            "user_id": str(p.user_id) if p.user_id else None,
        }

    return app


async def _user_with_pat(
    session: AsyncSession,
    *,
    superuser: bool = False,
    active: bool = True,
    expires_at: datetime | None = None,
    revoked: bool = False,
    last_used_at: datetime | None = None,
) -> tuple[User, str]:
    user = User(email=f"{uuid.uuid4().hex}@example.com", is_superuser=superuser, is_active=active)
    session.add(user)
    await session.flush()
    new = generate_secret(SecretKind.PAT)
    session.add(
        PersonalAccessToken(
            user_id=user.id,
            name="t",
            token_hash=new.hash,
            display_prefix=new.display_prefix,
            expires_at=expires_at,
            revoked_at=datetime.now(UTC) if revoked else None,
            last_used_at=last_used_at,
        )
    )
    await session.flush()
    return user, new.secret


@pytest.fixture
async def committed(engine):
    """A session whose commits are visible to the HTTP app (the test DB is dropped at the
    end of the run, so rows are not cleaned up).
    """
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


async def test_current_principal(settings: Settings, committed: AsyncSession) -> None:
    team = await factories.make_team(committed)
    user, pat = await _user_with_pat(committed)
    await factories.make_member(committed, team, user)
    _, su_pat = await _user_with_pat(committed, superuser=True)
    _, inactive = await _user_with_pat(committed, active=False)
    _, expired = await _user_with_pat(committed, expires_at=datetime.now(UTC) - timedelta(1))
    _, revoked = await _user_with_pat(committed, revoked=True)
    await committed.commit()

    with TestClient(_whoami_app(settings)) as client:

        def call(token: str | None):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return client.get("/whoami", headers=headers)

        r = call(pat)
        assert r.status_code == 200
        assert r.json() == {
            "kind": "user",
            "user_id": str(user.id),
        }
        assert call(su_pat).json()["kind"] == PrincipalKind.USER.value
        boot = call("test-bootstrap-token").json()
        assert boot == {"kind": "bootstrap", "user_id": None}

        for bad in (None, inactive, expired, revoked, "lwp-unknown", "test-bootstrap-tokenx"):
            r = call(bad)
            assert r.status_code == 401, bad
            assert r.json()["error"]["code"] == "unauthorized"
            assert r.headers["www-authenticate"] == "Bearer"

    async with AsyncSession(committed.bind) as s:
        used = await s.scalar(
            select(PersonalAccessToken.last_used_at).where(
                PersonalAccessToken.token_hash == hash_secret(pat)
            )
        )
        assert used is not None


def _transactional_app(settings: Settings) -> FastAPI:
    """An endpoint shaped like C2 CRUD: auth + request session + its own transaction."""
    app = create_app(settings)

    @app.post("/write")
    async def write(
        p: CurrentPrincipal, session: Annotated[AsyncSession, Depends(get_session)]
    ) -> dict:
        was_in_tx = session.in_transaction()
        async with session.begin():
            audit.record(
                session,
                action="test.write",
                resource_type="test",
                principal=p,
            )
        return {"was_in_tx": was_in_tx}

    return app


async def _last_used(engine, pat: str) -> datetime | None:
    async with AsyncSession(engine) as s:
        return await s.scalar(
            select(PersonalAccessToken.last_used_at).where(
                PersonalAccessToken.token_hash == hash_secret(pat)
            )
        )


async def test_auth_leaves_request_session_to_handler(
    settings: Settings, committed: AsyncSession, engine
) -> None:
    now = datetime.now(UTC)
    recent_at = now - timedelta(seconds=10)
    stale_at = now - timedelta(hours=2)
    _, never = await _user_with_pat(committed)
    _, recent = await _user_with_pat(committed, last_used_at=recent_at)
    _, stale = await _user_with_pat(committed, last_used_at=stale_at)
    await committed.commit()

    with TestClient(_transactional_app(settings)) as client:
        for token in (never, recent, stale, "test-bootstrap-token"):
            # Twice: the second call sees last_used_at freshly set by the first.
            for _ in range(2):
                r = client.post("/write", headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 200, (token[:8], r.text)
                assert r.json() == {"was_in_tx": False}

    assert await _last_used(engine, never) is not None
    assert await _last_used(engine, recent) == recent_at
    assert await _last_used(engine, stale) > stale_at
