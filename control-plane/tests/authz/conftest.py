"""Isolated grants for external authorization contracts."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

from lumewarp_control_plane.authz import INSTALLATION, Decision, Relation, team_resource
from lumewarp_control_plane.authz.contract_tests import AuthzSeed
from lumewarp_control_plane.authz.openfga import OpenFGABackend
from lumewarp_control_plane.settings import Settings


@pytest.fixture
def authz_seed():
    # External IAM owns deactivation: inactive users have no tuples.
    return AuthzSeed(
        *(str(uuid.uuid4()) for _ in range(5)),
        teams=(uuid.uuid4(), uuid.uuid4()),
    )


@pytest.fixture
def grant_check(authz_seed):
    seed = authz_seed
    grants = {
        (seed.superuser, "superuser", "installation", "default"),
        (seed.admin, "admin", "team", str(seed.teams[0])),
        (seed.member, "member", "team", str(seed.teams[0])),
    }

    async def check(user, relation, resource):
        allowed = (user, "superuser", "installation", "default") in grants or (
            (user, relation, resource.type, resource.id) in grants
            or (
                relation is Relation.MEMBER
                and (user, "admin", resource.type, resource.id) in grants
            )
        )
        return Decision.ALLOW if allowed else Decision.DENY

    return check


@pytest.fixture(scope="session")
async def openfga_settings():
    url = os.environ.get("LUMEWARP_TEST_OPENFGA_URL")
    if not url:
        pytest.skip("set LUMEWARP_TEST_OPENFGA_URL to run live OpenFGA tests")
    model = Path(__file__).resolve().parents[3] / "deploy/openfga/model.fga"
    process = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "-v",
        f"{model}:/model.fga:ro",
        "openfga/cli:v0.8.2",
        "model",
        "transform",
        "--file",
        "/model.fga",
        "--output-format",
        "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    async with httpx.AsyncClient(base_url=url) as client:
        response = await client.post("/stores", json={"name": f"lumewarp-test-{uuid.uuid4()}"})
        response.raise_for_status()
        store = response.json()["id"]
        try:
            response = await client.post(
                f"/stores/{store}/authorization-models", json=json.loads(stdout)
            )
            response.raise_for_status()
            yield Settings(
                openfga_url=url,
                openfga_store_id=store,
                openfga_model_id=response.json()["authorization_model_id"],
            )
        finally:
            response = await client.delete(f"/stores/{store}")
            response.raise_for_status()


@pytest.fixture
async def live_backend(openfga_settings, authz_seed):
    backend = OpenFGABackend(openfga_settings)
    seed = authz_seed
    tuples = [backend._tuple(seed.superuser, Relation.SUPERUSER, INSTALLATION)]
    tuples += [
        {"user": "installation:default", "relation": "installation", "object": f"team:{team}"}
        for team in seed.teams
    ]
    tuples += [
        backend._tuple(seed.admin, Relation.ADMIN, team_resource(seed.teams[0])),
        backend._tuple(seed.member, Relation.MEMBER, team_resource(seed.teams[0])),
    ]
    try:
        await backend._post("write", {"writes": {"tuple_keys": tuples}})
        yield backend
    finally:
        await backend._post("write", {"deletes": {"tuple_keys": tuples}})
        await backend.aclose()
