"""The default org: created on first use, never seeded by a migration."""

import pytest
from sqlalchemy import delete, select

from lumewarp_control_plane.errors import ApiError
from lumewarp_control_plane.models import Org, Team
from lumewarp_control_plane.orgs import DEFAULT_ORG_NAME, default_org, live_orgs
from tests import factories


@pytest.fixture
async def empty_hierarchy(session):
    """No orgs at all, in a transaction the test rolls back."""
    await session.execute(delete(Team))
    await session.execute(delete(Org))
    await session.flush()
    yield session
    await session.rollback()


async def test_first_team_creation_creates_the_default_org(empty_hierarchy) -> None:
    session = empty_hierarchy
    assert await live_orgs(session) == []
    org = await default_org(session)
    assert (org.name, org.deleted_at) == (DEFAULT_ORG_NAME, None)
    # Idempotent within the same request: the next team uses the same org.
    assert (await default_org(session)).id == org.id
    team = await factories.make_team(session)
    assert team.org_id == org.id
    assert len(await live_orgs(session)) == 1


async def test_default_org_prefers_its_name_over_the_only_live_org(empty_hierarchy) -> None:
    session = empty_hierarchy
    renamed = await factories.make_org(session, "only-org")
    assert (await default_org(session)).id == renamed.id
    preferred = await factories.make_org(session, DEFAULT_ORG_NAME)
    assert (await default_org(session)).id == preferred.id


async def test_several_orgs_without_a_default_are_ambiguous(empty_hierarchy) -> None:
    session = empty_hierarchy
    await factories.make_org(session, "org-a")
    await factories.make_org(session, "org-b")
    with pytest.raises(ApiError) as exc:
        await default_org(session)
    assert exc.value.status == 409
    # Nothing was created behind the refusal.
    assert await session.scalar(select(Org).where(Org.name == DEFAULT_ORG_NAME)) is None
