"""Upgrading a database written by v0.2.1 (revision 0001) keeps its rows.

The rows are inserted with raw SQL at 0001, so the test never depends on today's models
for the old schema. After `upgrade head` they must still be there, every existing key must
inherit its owner's models, and the compiled key policy must be org ∩ team ∩ owner.
"""

import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lumewarp_control_plane import migrate
from lumewarp_control_plane.compiler import compile_scope
from tests.conftest import create_database, drop_database

SCOPE = "upgrade-test"


def _id() -> uuid.UUID:
    return uuid.uuid4()


async def test_upgrade_from_0001_keeps_rows_and_compiles_the_same_policy() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "0001")
        ids = {
            name: _id()
            for name in (
                "org",
                "team",
                "user",
                "account",
                "upstream",
                "user_key",
                "account_key",
            )
        }
        names = {name: _id() for name in ("a", "b", "c", "d")}
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO scopes (name) VALUES (:s)"), {"s": SCOPE})
            await conn.execute(
                text(
                    "INSERT INTO upstreams (id, name, kind, base_url)"
                    " VALUES (:id, 'up', 'vllm', 'http://up/v1')"
                ),
                {"id": ids["upstream"]},
            )
            for name, name_id in names.items():
                await conn.execute(
                    text("INSERT INTO catalog_names (id, name, kind) VALUES (:id, :n, 'model')"),
                    {"id": name_id, "n": f"model-{name}"},
                )
                await conn.execute(
                    text(
                        "INSERT INTO models (name_id, upstream_id, upstream_model, model_type)"
                        " VALUES (:id, :up, :n, 'llm')"
                    ),
                    {"id": name_id, "up": ids["upstream"], "n": f"upstream-{name}"},
                )
            await conn.execute(
                text("INSERT INTO orgs (id, name, model_access) VALUES (:id, 'org', 'list')"),
                {"id": ids["org"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO teams (id, org_id, name, model_access)"
                    " VALUES (:id, :org, 'team', 'list')"
                ),
                {"id": ids["team"], "org": ids["org"]},
            )
            await conn.execute(
                text("INSERT INTO users (id, email) VALUES (:id, 'member@example.com')"),
                {"id": ids["user"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO team_members (team_id, user_id, role)"
                    " VALUES (:team, :user, 'member')"
                ),
                {"team": ids["team"], "user": ids["user"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO member_policies (team_id, user_id, model_access)"
                    " VALUES (:team, :user, 'list')"
                ),
                {"team": ids["team"], "user": ids["user"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO service_accounts (id, team_id, name) VALUES (:id, :team, 'robot')"
                ),
                {"id": ids["account"], "team": ids["team"]},
            )
            # org {a, b, c} ∩ team {a, b, d} ∩ member {a, d} = {a}; the account inherits
            # the team: {a, b}.
            grants = [
                ({"org_id": ids["org"]}, ("a", "b", "c")),
                ({"team_id": ids["team"]}, ("a", "b", "d")),
                ({"team_id": ids["team"], "user_id": ids["user"]}, ("a", "d")),
            ]
            for owner, granted in grants:
                for name in granted:
                    await conn.execute(
                        text(
                            "INSERT INTO policy_grants"
                            " (id, name_id, org_id, team_id, user_id, service_account_id)"
                            " VALUES (:id, :name, :org_id, :team_id, :user_id, NULL)"
                        ),
                        {
                            "id": _id(),
                            "name": names[name],
                            "org_id": owner.get("org_id"),
                            "team_id": owner.get("team_id"),
                            "user_id": owner.get("user_id"),
                        },
                    )
            for key, owner_kind, user_id, account_id in (
                ("user_key", "user", ids["user"], None),
                ("account_key", "service_account", None, ids["account"]),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO api_keys (id, team_id, owner_kind, user_id,"
                        " service_account_id, name, key_hash, display_prefix)"
                        " VALUES (:id, :team, :kind, :user, :account, :name, :hash, 'lw-x')"
                    ),
                    {
                        "id": ids[key],
                        "team": ids["team"],
                        "kind": owner_kind,
                        "user": user_id,
                        "account": account_id,
                        "name": key,
                        "hash": hashlib.sha256(key.encode()).digest(),
                    },
                )

        await migrate.upgrade(engine, "head")

        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM policy_grants")) == 8
            rows = (
                await conn.execute(text("SELECT id, model_access FROM api_keys ORDER BY name"))
            ).all()
            assert {row.id: row.model_access for row in rows} == {
                ids["user_key"]: "inherit",
                ids["account_key"]: "inherit",
            }
            for table in ("orgs", "teams", "member_policies", "service_accounts", "team_members"):
                assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 1

        async with AsyncSession(engine, expire_on_commit=False) as session:
            compiled = await compile_scope(session, SCOPE, require_local_membership=True)
        policies = {key.key_id: key.policy for key in compiled.snapshot.api_keys}
        assert list(policies[str(ids["user_key"])].allowed_models) == ["model-a"]
        assert list(policies[str(ids["account_key"])].allowed_models) == ["model-a", "model-b"]
        assert not policies[str(ids["user_key"])].allow_all_models

        # The new owner shape works on the upgraded table, and the replaced CHECK still
        # refuses a grant without an owner.
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO policy_grants (id, name_id, key_id) VALUES (:id, :n, :k)"),
                {"id": _id(), "n": names["a"], "k": ids["user_key"]},
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO policy_grants (id, name_id) VALUES (:id, :n)"),
                    {"id": _id(), "n": names["b"]},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE api_keys SET model_access = 'all' WHERE id = :k"),
                    {"k": ids["user_key"]},
                )
    finally:
        await engine.dispose()
        await drop_database(url)


async def test_downgrade_is_refused() -> None:
    url = await create_database()
    engine = create_async_engine(url)
    try:
        await migrate.upgrade(engine, "head")
        with pytest.raises(NotImplementedError, match="restore"):
            await migrate.downgrade(engine, "0001")
    finally:
        await engine.dispose()
        await drop_database(url)
