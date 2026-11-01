"""External IAM key ownership remains eligible through compile and routing rollback."""

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumewarp_control_plane import app as app_module
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.authz import Authorizer, Decision, Relation
from lumewarp_control_plane.compiler import compile_scope, publish_pending
from lumewarp_control_plane.models import ConfigVersion, Scope, TeamMember
from tests.api.helpers import BOOTSTRAP_AUTH, auth, suffix, user_with_token
from tests.factories import make_team


async def test_external_member_key_create_compile_and_rollback(
    committed, api_engine, api_settings, monkeypatch
) -> None:
    settings = api_settings.model_copy(
        update={"scope": f"external-{suffix()}", "authz_backend": "external"}
    )
    committed.add(Scope(name=settings.scope))
    team = await make_team(committed, model_access="list", allowed_models=["a"], rpm_limit=60)
    user, token = await user_with_token(committed)
    await committed.commit()

    async def check(user_id, relation, resource):
        if user_id == str(user.id) and relation is Relation.MEMBER and resource.id == str(team.id):
            return Decision.ALLOW
        return Decision.DENY

    monkeypatch.setattr(app_module, "load_authorizer", lambda *_args: Authorizer(check, "external"))
    factory = async_sessionmaker(api_engine, expire_on_commit=False)
    with TestClient(app_module.create_app(settings)) as client:
        response = client.post(
            f"/api/v1/teams/{team.id}/keys", json={"name": "external"}, headers=auth(token)
        )
        assert response.status_code == 201, response.text
        key_id = response.json()["key"]["id"]
        async with AsyncSession(api_engine) as session:
            assert await session.get(TeamMember, (team.id, user.id)) is None
            compiled = await compile_scope(session, settings.scope, require_local_membership=False)
        wire = next(k for k in compiled.snapshot.api_keys if k.key_id == key_id)
        assert wire.status == pb.KEY_STATUS_ACTIVE
        assert list(wire.policy.allowed_models) == ["a"]
        assert [r.id for r in wire.policy.rate_limit_scopes] == [f"team:{team.id}"]
        assert await publish_pending(factory, settings.scope, require_local_membership=False) == 1
        rollback = client.post(
            "/api/v1/config/rollback", json={"version": 1}, headers=BOOTSTRAP_AUTH
        )
        assert rollback.status_code == 200, rollback.text
        async with AsyncSession(api_engine) as session:
            version = await session.get(ConfigVersion, (settings.scope, rollback.json()["version"]))
        snap = pb.Snapshot.FromString(version.payload)
        assert next(k for k in snap.api_keys if k.key_id == key_id).status == pb.KEY_STATUS_ACTIVE
