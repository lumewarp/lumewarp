"""Minimal row builders for tests."""

from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import ApiKey, Org, Project, Scope, Team
from lumewarp_control_plane.security import SecretKind, generate_secret


async def scope(session: AsyncSession, name: str = "default") -> Scope:
    row = Scope(name=name)
    session.add(row)
    await session.flush()
    return row


async def hierarchy(session: AsyncSession) -> tuple[Org, Team, Project]:
    suffix = uuid7().hex[-8:]
    org = Org(name=f"org-{suffix}")
    session.add(org)
    await session.flush()
    team = Team(org_id=org.id, name="team")
    session.add(team)
    await session.flush()
    project = Project(org_id=org.id, team_id=team.id, name="project")
    session.add(project)
    await session.flush()
    return org, team, project


def api_key(project: Project, **kw) -> ApiKey:
    new = generate_secret(SecretKind.API_KEY)
    return ApiKey(
        org_id=project.org_id,
        team_id=project.team_id,
        project_id=project.id,
        name=kw.pop("name", "key"),
        key_hash=kw.pop("key_hash", new.hash),
        display_prefix=new.display_prefix,
        **kw,
    )
