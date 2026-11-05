"""Installation-level roles: Postgres is the only source.

A `proxy_admin` is unique and an `admin` may be assigned to many users. The authorization
backend is never asked about installation-level questions; it only answers team
relations. `RequestAuthz` reads this table once per request, so a revoked role applies to
the caller's next request.

All installation-role mutations (first-login bootstrap, assign, remove, transfer) take the
same transaction-scoped advisory lock. The table's partial unique index on `proxy_admin`
stays as the last line of defence.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.errors import unauthorized
from lumewarp_control_plane.models import (
    InstallationAdmin,
    InstallationRole,
    PersonalAccessToken,
    User,
    UserSession,
)
from lumewarp_control_plane.security import Principal, PrincipalKind
from lumewarp_control_plane.settings import Settings

if TYPE_CHECKING:
    from lumewarp_control_plane.authz import RequestAuthz

# Arbitrary constant for pg_advisory_xact_lock; it only has to be stable in this codebase.
INSTALLATION_LOCK = 0x4C57_0001


async def lock_installation(session: AsyncSession) -> None:
    """Serialize installation-role changes for the rest of the transaction."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INSTALLATION_LOCK})


async def role_of(session: AsyncSession, user_id: uuid.UUID) -> InstallationRole | None:
    return await session.scalar(
        select(InstallationAdmin.role).where(InstallationAdmin.user_id == user_id)
    )


async def revoke_user_pats(
    session: AsyncSession, user_id: uuid.UUID, *, principal: Principal
) -> int:
    """Revoke every PAT of a user who is being promoted to proxy_admin.

    Personal access tokens have no issuer column: a token another installation admin issued
    before the promotion would otherwise carry the new role. Revoke all of them in the same
    transaction as the promotion; the user can mint a fresh one afterwards (and a new login
    session is created after this call). One audit row records the count only.
    """
    result = await session.execute(
        update(PersonalAccessToken)
        .where(
            PersonalAccessToken.user_id == user_id,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    revoked = int(result.rowcount or 0)
    audit.record(
        session,
        action="installation_admin.revoke_tokens",
        resource_type="installation_admin",
        resource_id=user_id,
        principal=principal,
        details={"user_id": user_id, "revoked": revoked},
    )
    return revoked


async def recheck_principal(session: AsyncSession, principal: Principal) -> None:
    """Revalidate the caller's own credential after taking the installation lock.

    Authentication happens before the lock. A PAT revoked (or a session closed) while this
    request waited must not keep acting as its owner; a concurrent promotion revokes the
    user's PATs, so the pending request must answer 401. About to issue a PAT, change a
    role, transfer the proxy_admin or write a protected account, the caller's session/PAT
    and active user are re-read inside the lock. A session of the user themself is not
    revoked by a promotion and therefore stays valid.
    """
    now = datetime.now(UTC)
    if principal.is_break_glass:
        if principal.token_id is None:
            raise unauthorized()
        credential = await session.get(UserSession, principal.token_id, populate_existing=True)
        if (
            credential is None
            or credential.kind != PrincipalKind.BREAK_GLASS.value
            or credential.revoked_at is not None
            or credential.expires_at <= now
        ):
            raise unauthorized()
        return
    if principal.user_id is None or principal.token_id is None:
        raise unauthorized()
    if principal.kind is PrincipalKind.USER:
        token = await session.get(PersonalAccessToken, principal.token_id, populate_existing=True)
        if (
            token is None
            or token.user_id != principal.user_id
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now)
        ):
            raise unauthorized()
    elif principal.kind is PrincipalKind.SESSION:
        credential = await session.get(UserSession, principal.token_id, populate_existing=True)
        if (
            credential is None
            or credential.user_id != principal.user_id
            or credential.revoked_at is not None
            or credential.expires_at <= now
        ):
            raise unauthorized()
    user = await session.get(User, principal.user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise unauthorized()


async def recheck_role(session: AsyncSession, authz: RequestAuthz) -> InstallationRole | None:
    """Re-read the caller's installation role in the locked session and cache it.

    A role removed while the request waited for the installation lock must fail the
    re-required action. Break-glass has no role row; `is_admin()` short-circuits it.
    """
    if authz.principal.is_break_glass:
        return None
    assert authz.user_id is not None
    role = await role_of(session, authz.user_id)
    authz.set_installation_role(role)
    return role


async def proxy_admin_id(session: AsyncSession) -> uuid.UUID | None:
    return await session.scalar(
        select(InstallationAdmin.user_id).where(
            InstallationAdmin.role == InstallationRole.PROXY_ADMIN
        )
    )


def matches_initial_admin(settings: Settings, email: str | None) -> bool:
    configured = (settings.initial_admin_email or "").strip()
    return bool(configured) and bool(email) and email.strip().lower() == configured.lower()


async def prepare_bootstrap_lock(
    session: AsyncSession, claims: Mapping[str, object], settings: Settings
) -> bool:
    """Take the installation lock before the login reads or locks the user, if needed.

    The callback calls this before `resolve_user`, so every path that can link or lock a
    User row and later bootstrap uses one order: installation lock, then the User row.
    Ordinary logins (no setting, a different claim email, or a proxy_admin already exists)
    do not take the lock. A True result means bootstrap may run; `bootstrap_initial_admin`
    still re-checks every condition under the lock.
    """
    claim_email = claims.get("email")
    if claims.get("email_verified") is not True:
        return False
    if not isinstance(claim_email, str) or not matches_initial_admin(settings, claim_email):
        return False
    if await proxy_admin_id(session) is not None:
        return False
    await lock_installation(session)
    return True


async def bootstrap_initial_admin(
    session: AsyncSession,
    claims: Mapping[str, object],
    user: User,
    settings: Settings,
    *,
    locked: bool = False,
) -> bool:
    """Make `user` the proxy_admin when this login is the configured first one.

    Runs inside the login transaction. The IdP email claim must be verified and match the
    setting; the persisted user is re-read under the installation lock, so a concurrent
    email change or deactivation that committed first is respected. `locked=True` means
    the caller already took the lock via `prepare_bootstrap_lock` before `resolve_user`.
    The partial unique index remains the invariant.
    """
    claim_email = claims.get("email")
    if claims.get("email_verified") is not True:
        return False
    if not isinstance(claim_email, str) or not matches_initial_admin(settings, claim_email):
        return False
    if not locked:
        await lock_installation(session)
    # Do not trust the identity-map copy that resolve_user returned before the lock.
    persisted = await session.get(User, user.id, with_for_update=True, populate_existing=True)
    if persisted is None or not persisted.is_active:
        return False
    if not matches_initial_admin(settings, persisted.email):
        return False
    if await proxy_admin_id(session) is not None:
        return False
    row = await session.get(InstallationAdmin, persisted.id)
    if row is None:
        session.add(InstallationAdmin(user_id=persisted.id, role=InstallationRole.PROXY_ADMIN))
    elif row.role is InstallationRole.ADMIN:
        row.role = InstallationRole.PROXY_ADMIN
        row.assigned_at = datetime.now(UTC)
    else:
        return False
    await session.flush()
    principal = Principal(kind=PrincipalKind.USER, user_id=persisted.id)
    # A token another admin issued before this login must not inherit the new role.
    await revoke_user_pats(session, persisted.id, principal=principal)
    audit.record(
        session,
        action="installation_admin.bootstrap",
        resource_type="installation_admin",
        resource_id=persisted.id,
        principal=principal,
        details={"user_id": persisted.id, "role": InstallationRole.PROXY_ADMIN.value},
    )
    return True
