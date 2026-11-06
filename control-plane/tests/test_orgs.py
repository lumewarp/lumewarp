"""Orgs: live listing and the name rules that archiving changes."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from lumewarp_control_plane.models import Org
from lumewarp_control_plane.orgs import live_orgs
from tests import factories


async def test_live_orgs_excludes_archived_orgs(session) -> None:
    live = await factories.make_org(session)
    archived = await factories.make_org(session)
    archived.deleted_at = datetime.now(UTC)
    await session.flush()
    listed = {row.id for row in await live_orgs(session)}
    assert live.id in listed and archived.id not in listed
    assert (await session.get(Org, archived.id)) is not None


async def test_team_name_is_unique_among_live_teams(session) -> None:
    org = await factories.make_org(session)
    name = "same-name"
    await factories.make_team(session, name, org=org)
    with pytest.raises(IntegrityError):
        await factories.make_team(session, name, org=org)


async def test_archiving_frees_the_name_for_a_new_team(session) -> None:
    org = await factories.make_org(session)
    name = "reusable"
    old = await factories.make_team(session, name, org=org)
    old.deleted_at = datetime.now(UTC)
    await session.flush()
    # A second live team with the same name is accepted: the unique index covers live rows
    # only, so history keeps its old row while the name is reusable.
    new = await factories.make_team(session, name, org=org)
    assert new.id != old.id
