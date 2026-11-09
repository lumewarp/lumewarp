"""Effective catalog and live authorization checks for Playground keys."""

from datetime import UTC, datetime

from sqlalchemy import delete

from lumewarp_control_plane.models import ModelAccess, TeamMember
from tests.api.test_playground import setup_keys
from tests.factories import make_key, make_member, make_model, make_team


async def test_effective_models_and_live_team(client, committed):
    own, _, headers, _ = await setup_keys(committed)
    team = await make_team(
        committed, model_access=ModelAccess.LIST, allowed_models=["visible", "team-only"]
    )
    await make_model(committed, "visible")
    # Reuse the same session user, but narrow this team's member policy.
    from lumewarp_control_plane.models import User

    user = await committed.get(User, own.user_id)
    await make_member(
        committed, team, user, model_access=ModelAccess.LIST, allowed_models=["visible"]
    )
    key = await make_key(committed, team, user)
    await committed.commit()
    response = client.get("/api/v1/playground", headers=headers)
    entry = next(item for item in response.json()["keys"] if item["id"] == str(key.id))
    assert entry["models"] == ["visible"]

    team.deleted_at = datetime.now(UTC)
    await committed.commit()
    response = client.get("/api/v1/playground", headers=headers)
    assert str(key.id) not in [item["id"] for item in response.json()["keys"]]
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(key.id)},
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "key not found"

    # A still-active key must not authorize a former member before reconciliation.
    await committed.execute(
        delete(TeamMember).where(
            TeamMember.team_id == own.team_id, TeamMember.user_id == own.user_id
        )
    )
    await committed.commit()
    response = client.get("/api/v1/playground", headers=headers)
    assert response.json()["keys"] == []
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={},
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "key not found"


async def test_only_llm_and_typed_effective_models(client, committed):
    from pydantic import SecretStr

    from lumewarp_control_plane.models import ModelType
    from tests.factories import make_alias, make_router

    own, _, headers, _ = await setup_keys(committed)
    llm = await make_model(committed, "chat")
    embedding = await make_model(committed, "embed", model_type=ModelType.EMBEDDING)
    await make_alias(committed, "chat-alias", llm)
    await make_alias(committed, "embed-alias", embedding)
    await make_router(committed, "chat-router", [llm])
    await make_router(committed, "embed-router", [embedding])
    await committed.commit()
    response = client.get("/api/v1/playground", headers=headers)
    models = response.json()["keys"][0]["models"]
    assert {"chat", "chat-alias", "chat-router"} <= set(models)
    assert not {"embed", "embed-alias", "embed-router"} & set(models)
    me = client.get("/api/v1/users/me", headers=headers).json()
    team = next(t for t in me["teams"] if t["team_id"] == str(own.team_id))
    typed = {m["name"]: m["model_type"] for m in team["typed_models"]}
    assert typed["chat-router"] == "llm" and typed["embed-alias"] == "embedding"
    assert set(team["models"]) == set(typed)
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    client.app.state.settings.encryption_key = SecretStr("x" * 32)
    for model in ("embed", "embed-alias", "embed-router"):
        response = client.post(
            "/api/v1/playground/v1/chat/completions",
            headers={**headers, "x-lumewarp-act-as": str(own.id)},
            json={"model": model},
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "model_type_mismatch"


async def test_missing_encryption_key_disables(client, committed):
    own, _, headers, _ = await setup_keys(committed)
    client.app.state.settings.playground_gateway_url = "http://gateway:4000"
    client.app.state.settings.encryption_key = None
    assert client.get("/api/v1/playground", headers=headers).json()["enabled"] is False
    response = client.post(
        "/api/v1/playground/v1/chat/completions",
        headers={**headers, "x-lumewarp-act-as": str(own.id)},
        json={},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "playground_disabled"
