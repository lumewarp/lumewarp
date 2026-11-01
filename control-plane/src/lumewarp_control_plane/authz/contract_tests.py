"""Reusable backend contract.

Subclass ``AuthzContract`` in a pytest test module and supply ``authz_backend`` (a
backend object or a plain async check function) and ``authz_seed`` fixtures.
The seed contains active outsider/admin/member users, an inactive user with
admin grants, an unknown user, and two existing teams. Only the first
team grants the admin and member their respective relations.
"""

from dataclasses import dataclass
from uuid import UUID

import pytest

from lumewarp_control_plane.authz import (
    Authorizer,
    Decision,
    Relation,
    team_resource,
)


@dataclass(frozen=True)
class AuthzSeed:
    outsider: str
    admin: str
    member: str
    inactive: str
    unknown: str
    teams: tuple[UUID, UUID]


class AuthzContract:
    async def test_implied_relations(self, authz_backend, authz_seed: AuthzSeed) -> None:
        authz = Authorizer(authz_backend)
        seed = authz_seed
        for relation in (Relation.ADMIN, Relation.MEMBER):
            assert (
                await authz.check(seed.admin, relation, team_resource(seed.teams[0]))
                is Decision.ALLOW
            )
        assert (
            await authz.check(seed.member, Relation.MEMBER, team_resource(seed.teams[0]))
            is Decision.ALLOW
        )
        assert (
            await authz.check(seed.member, Relation.ADMIN, team_resource(seed.teams[0]))
            is Decision.DENY
        )
        for user in (seed.admin, seed.member):
            for relation in (Relation.ADMIN, Relation.MEMBER):
                assert (
                    await authz.check(user, relation, team_resource(seed.teams[1])) is Decision.DENY
                )

    async def test_inactive_and_unknown_denied(self, authz_backend, authz_seed: AuthzSeed) -> None:
        authz = Authorizer(authz_backend)
        for user in (authz_seed.inactive, authz_seed.unknown):
            for team in authz_seed.teams:
                for relation in (Relation.ADMIN, Relation.MEMBER):
                    assert await authz.check(user, relation, team_resource(team)) is Decision.DENY

    async def test_batch_order(self, authz_backend, authz_seed: AuthzSeed) -> None:
        authz = Authorizer(authz_backend)
        seed = authz_seed
        team = team_resource(seed.teams[0])
        items = [
            (seed.member, Relation.ADMIN, team),
            (seed.admin, Relation.MEMBER, team),
            (seed.unknown, Relation.MEMBER, team),
            (seed.outsider, Relation.MEMBER, team),
            (seed.inactive, Relation.ADMIN, team),
            (seed.member, Relation.MEMBER, team),
        ]
        assert await authz.batch_check(items) == [
            Decision.DENY,
            Decision.ALLOW,
            Decision.DENY,
            Decision.DENY,
            Decision.DENY,
            Decision.ALLOW,
        ]
        assert await authz.batch_check([]) == []

    @pytest.mark.parametrize("relation", [Relation.ADMIN, Relation.MEMBER])
    async def test_list_teams_consistent(
        self, authz_backend, authz_seed: AuthzSeed, relation: Relation
    ) -> None:
        authz = Authorizer(authz_backend)
        seed = authz_seed
        for user in (seed.outsider, seed.admin, seed.member, seed.inactive, seed.unknown):
            expected = {
                team
                for team in seed.teams
                if await authz.check(user, relation, team_resource(team)) is Decision.ALLOW
            }
            assert await authz.list_teams(user, relation, iter(seed.teams)) == expected
            assert await authz.list_teams(user, relation, [seed.teams[1]]) == (
                expected & {seed.teams[1]}
            )
            assert await authz.list_teams(user, relation, []) == set()

    async def test_errors_fail_closed(
        self, authz_backend, authz_seed: AuthzSeed, monkeypatch
    ) -> None:
        authz = Authorizer(authz_backend)

        async def unavailable(*args):
            raise RuntimeError("backend unavailable")

        monkeypatch.setattr(authz.backend, "check", unavailable)
        for method in ("batch_check", "list_teams"):
            if hasattr(authz.backend, method):
                monkeypatch.setattr(authz.backend, method, unavailable)
        item = (authz_seed.member, Relation.MEMBER, team_resource(authz_seed.teams[0]))
        assert await authz.check(*item) is Decision.ERROR
        assert await authz.batch_check([item, item]) == [Decision.ERROR, Decision.ERROR]
        assert await authz.list_teams(item[0], item[1], authz_seed.teams) is None
