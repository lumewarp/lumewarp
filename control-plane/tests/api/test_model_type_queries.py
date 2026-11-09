"""Catalog type resolution stays bounded on list and chat paths."""

import pytest
from sqlalchemy import event

from lumewarp_control_plane.model_types import called_name_type
from lumewarp_control_plane.models import ModelType
from tests.api.helpers import ADMIN_AUTH, suffix
from tests.api.test_playground import setup_keys
from tests.factories import make_alias, make_key, make_model, make_router


async def test_alias_list_type_queries_are_constant(client, committed):
    model = await make_model(committed, suffix())
    for i in range(5):
        await make_alias(committed, f"{suffix()}-{i}", model)
    await committed.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM models" in statement:
            statements.append(statement)

    engine = client.app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for limit in (1, 50):
            statements.clear()
            response = client.get(f"/api/v1/aliases?limit={limit}", headers=ADMIN_AUTH)
            assert response.status_code == 200, response.text
            assert len(statements) == 1
            assert all(row["model_type"] == "llm" for row in response.json()["items"])
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_playground_loads_types_once_for_many_keys(client, committed):
    from lumewarp_control_plane.models import Team, User

    own, _, headers, _ = await setup_keys(committed)
    team = await committed.get(Team, own.team_id)
    user = await committed.get(User, own.user_id)
    for _ in range(4):
        await make_key(committed, team, user)
    await committed.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "models.model_type" in statement and "FROM models" in statement:
            statements.append(statement)

    engine = client.app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/v1/playground", headers=headers)
        assert response.status_code == 200, response.text
        assert len(response.json()["keys"]) >= 5
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("kind", list(ModelType))
async def test_one_called_name_type_is_one_query(committed, kind):
    names = [suffix() for _ in range(4)]
    model = await make_model(committed, names[0], model_type=kind)
    router = await make_router(committed, names[1], [model])
    await make_alias(committed, names[2], model)
    await make_alias(committed, names[3], router)
    await committed.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = committed.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for name in (*names, "missing"):
            statements.clear()
            assert await called_name_type(committed, name) == (None if name == "missing" else kind)
            assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", capture)
