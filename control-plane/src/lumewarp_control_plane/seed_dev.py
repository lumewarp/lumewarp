"""Seed a development control plane with the demo, a realistic catalog and 30 days of usage.

`lumewarp-control-plane seed-dev` is development-only: it refuses to run unless
`LUMEWARP_DEV_MODE` is on and the database URL points at loopback. The catalog is written
through the HTTP API as the break-glass admin, so its rows get real audit entries (the one
exception is the org-admin grant, which the API refuses until the account signs in once);
the history is ingested in-process through `usage.ingest`, the
same path a gateway report takes, so `usage_totals` and the budget views stay coherent.

Each run deletes only the seed teams' `usage_events` and re-ingests a window ending now,
then rebuilds those scopes' `usage_totals` from the events, so the history slides forward
and a second run changes nothing but the anchor time. Key secrets are written to
`.dev/seed-keys` (mode 600) and never stored in the database.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import usage
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2
from lumewarp_control_plane.db import create_engine, create_session_factory
from lumewarp_control_plane.models import (
    KeyOwnerKind,
    OrgAdmin,
    ScopeType,
    ServiceAccount,
    TeamMember,
    TrafficKind,
    UsageEvent,
    UsageTotal,
    member_scope_id,
)
from lumewarp_control_plane.settings import Settings
from lumewarp_control_plane.windows import month_window

ZONES = ("default", "prod")
DEFAULT_URL = "http://127.0.0.1:8091"
MOCK_BASE_URL = "http://127.0.0.1:9100/v1"
MCP_URL = "http://127.0.0.1:9110/mcp"
A2A_URL = "http://127.0.0.1:9120/a2a"
GROUP_NAME = "chat-models"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    model_type: str
    upstream_model: str
    price_in: int | None
    price_out: int | None
    price_audio: int | None = None


MODELS = (
    ModelSpec("llama-3.1-8b", "llm", "meta-llama/Llama-3.1-8B-Instruct", 200_000_000, 600_000_000),
    ModelSpec("qwen2.5-32b", "llm", "Qwen/Qwen2.5-32B-Instruct", 500_000_000, 1_500_000_000),
    ModelSpec("gpt-4o-mini", "llm", "openai/gpt-4o-mini", 2_000_000_000, 6_000_000_000),
    # An embedding has no output tokens to price.
    ModelSpec("bge-m3", "embedding", "BAAI/bge-m3", 100_000_000, 0),
    ModelSpec(
        "whisper-large-v3",
        "transcription",
        "openai/whisper-large-v3",
        100_000_000,
        300_000_000,
        1_000_000,
    ),
)
MODEL_NAMES = {spec.name: spec for spec in MODELS}
LLM_NAMES = ("llama-3.1-8b", "qwen2.5-32b", "gpt-4o-mini")
FALLBACK_MODEL = "gpt-4o-mini"


@dataclass(frozen=True)
class UserSpec:
    email: str
    display_name: str
    team: str
    role: str


@dataclass(frozen=True)
class TeamSpec:
    name: str
    org: str
    service_account: str
    models: tuple[str, ...]


USERS = (
    UserSpec("bob@example.com", "Bob Chen", "search", "member"),
    UserSpec("carol@example.com", "Carol Diaz", "search", "admin"),
    UserSpec("dave@example.com", "Dave Okoro", "platform", "member"),
    UserSpec("erin@example.com", "Erin Walsh", "platform", "admin"),
    UserSpec("frank@example.com", "Frank Moreau", "ml-research", "member"),
    UserSpec("grace@example.com", "Grace Ito", "ml-research", "admin"),
)
ORG_ADMIN = "carol@example.com"

TEAMS = (
    TeamSpec(
        "search",
        "acme",
        "search-chat",
        ("chat", "chat-ab", "chat-default", "embed", "whisper-large-v3"),
    ),
    TeamSpec("platform", "acme", "platform-etl", ("chat", "chat-ab", "embed")),
    TeamSpec(
        "ml-research",
        "research",
        "research-batch",
        ("chat", "chat-default", "embed", "whisper-large-v3"),
    ),
)
ORGS = ("acme", "research")


@dataclass(frozen=True)
class SeedKey:
    """One seeded key: the traffic generator's unit of attribution."""

    zone: str
    team: str
    team_id: uuid.UUID
    key_id: uuid.UUID
    owner_id: uuid.UUID
    service_account: str


@dataclass
class SeedPlan:
    org_ids: dict[str, uuid.UUID]
    team_ids: dict[str, uuid.UUID]
    user_ids: dict[str, uuid.UUID]
    keys: list[SeedKey]
    # `api` when the org admin was granted over HTTP, `direct` when the row had to be
    # written because the API only grants it to a user who has signed in once, and
    # `external` when an authz backend owns the memberships.
    org_admin: str


class SeedRefused(RuntimeError):
    """A dev-only precondition failed; the run wrote nothing."""


# --------------------------------------------------------------------------- guard


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _guard(settings: Settings) -> None:
    """Refuse anything that is not a loopback dev database with a dev-mode password."""
    if not settings.dev_mode:
        raise SeedRefused("LUMEWARP_DEV_MODE must be true (seed-dev is development only)")
    url = make_url(settings.database_url)
    if not _loopback(url.host):
        raise SeedRefused(
            f"refusing a non-loopback database ({url.host or 'no host'});"
            " seed-dev writes fake usage rows"
        )
    if settings.break_glass_password is None:
        raise SeedRefused("signing in as break-glass needs LUMEWARP_BREAK_GLASS_PASSWORD")


# --------------------------------------------------------------------------- HTTP


class Api:
    """The admin API as the break-glass session, find-or-create by name."""

    def __init__(self, url: str, password: str) -> None:
        self._client = httpx.Client(
            base_url=url.rstrip("/") + "/api/v1", timeout=20.0, follow_redirects=True
        )
        try:
            response = self._client.post("/session/break-glass", json={"password": password})
        except httpx.HTTPError as exc:
            raise SeedRefused(f"control plane at {url} is unreachable: {exc}") from None
        if response.status_code >= 400:
            raise SeedRefused(f"break-glass login failed ({response.status_code})")
        self.csrf = self._call("GET", "/session")["csrf_token"]

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {} if method == "GET" else {"x-lumewarp-csrf": self.csrf}
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                code = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = ""
            if code == "managed_externally":
                raise ManagedExternally(path)
            raise ApiRefused(
                f"{method} {path}: {response.status_code} {response.text[:300]}", code
            ) from None
        return response.json() if response.content else {}

    def get(self, path: str, **params: Any) -> Any:
        return self._call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def get_items(self, path: str) -> list[dict[str, Any]]:
        return self.get(path, limit=200)["items"]

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", path, json=body)

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("PUT", path, json=body)

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("PATCH", path, json=body)


class ApiRefused(SeedRefused):
    """An API refusal with the response's stable error code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class ManagedExternally(ApiRefused):
    """The authz backend owns memberships, so the API cannot write them."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path}: memberships are managed by the authz backend", "managed_externally"
        )


def _find(items: Sequence[dict[str, Any]], **fields: Any) -> dict[str, Any] | None:
    for item in items:
        if all(item.get(key) == value for key, value in fields.items()):
            return item
    return None


def _ensure_org(api: Api, name: str) -> uuid.UUID:
    found = _find(api.get_items("/orgs"), name=name)
    return uuid.UUID(found["id"]) if found else uuid.UUID(api.post("/orgs", {"name": name})["id"])


def _ensure_user(api: Api, spec: UserSpec) -> uuid.UUID:
    found = _find(api.get_items("/users"), email=spec.email)
    if found:
        return uuid.UUID(found["id"])
    created = api.post("/users", {"email": spec.email, "display_name": spec.display_name})
    return uuid.UUID(created["id"])


def _ensure_team(api: Api, spec: TeamSpec, org_id: uuid.UUID) -> uuid.UUID:
    found = _find(api.get_items("/teams"), name=spec.name, org_id=str(org_id))
    if found:
        return uuid.UUID(found["id"])
    created = api.post("/teams", {"name": spec.name, "org_id": str(org_id)})
    return uuid.UUID(created["id"])


def _apply_team_policy(api: Api, team_id: uuid.UUID, spec: TeamSpec) -> None:
    api.patch(
        f"/teams/{team_id}",
        {
            "model_access": "list",
            "allowed_models": list(spec.models),
            "allowed_groups": [GROUP_NAME],
            "mcp_access": "list",
            "mcp_servers": [{"server": "example", "all_tools": True}],
            "a2a_access": "list",
            "a2a_agents": ["helper"],
        },
    )


def _add_member(api: Api, team_id: uuid.UUID, user_id: uuid.UUID, role: str) -> bool:
    try:
        api.put(f"/teams/{team_id}/members/{user_id}", {"role": role})
    except ManagedExternally:
        return False
    return True


def _add_org_admin(api: Api, org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Grant the org admin over HTTP; `direct` when only a first sign-in blocks the API."""
    try:
        api.put(f"/orgs/{org_id}/admins/{user_id}", {})
    except ManagedExternally:
        return "external"
    except ApiRefused as exc:
        if exc.code != "conflict":
            raise
        # 409: the API refuses to grant a role to an account that never signed in, which
        # no freshly created dev user has. The row itself is enough for the built-in
        # backend, so the caller writes it directly.
        return "direct"
    return "api"


async def _write_org_admin(settings: Settings, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            if await session.get(OrgAdmin, (org_id, user_id)) is None:
                session.add(OrgAdmin(org_id=org_id, user_id=user_id))
    finally:
        await engine.dispose()


def _ensure_service_account(api: Api, team_id: uuid.UUID, name: str) -> uuid.UUID:
    found = _find(api.get_items(f"/teams/{team_id}/service-accounts"), name=name)
    if found:
        return uuid.UUID(found["id"])
    created = api.post(f"/teams/{team_id}/service-accounts", {"name": name})
    return uuid.UUID(created["id"])


def _ensure_upstream(api: Api, zone: str) -> uuid.UUID:
    name = f"mock-{zone}"
    found = _find(api.get_items("/upstreams"), name=name, zone=zone)
    if found:
        return uuid.UUID(found["id"])
    created = api.post(
        "/upstreams",
        {
            "name": name,
            "zone": zone,
            "kind": "openai_compatible",
            "base_url": MOCK_BASE_URL,
        },
    )
    return uuid.UUID(created["id"])


def _ensure_model(api: Api, spec: ModelSpec) -> uuid.UUID:
    found = _find(api.get_items("/models"), name=spec.name)
    if found:
        return uuid.UUID(found["id"])
    body: dict[str, Any] = {
        "name": spec.name,
        "model_type": spec.model_type,
        "price_input_nano_usd_per_mtok": spec.price_in,
        "price_output_nano_usd_per_mtok": spec.price_out,
    }
    if spec.price_audio is not None:
        body["price_nano_usd_per_audio_second"] = spec.price_audio
    return uuid.UUID(api.post("/models", body)["id"])


def _ensure_alias(api: Api, name: str, target: uuid.UUID) -> None:
    found = _find(api.get_items("/aliases"), name=name)
    if found is None:
        api.post("/aliases", {"name": name, "target": str(target)})


def _ensure_router(api: Api, body: dict[str, Any]) -> uuid.UUID:
    found = _find(api.get_items("/routers"), name=body["name"])
    if found:
        return uuid.UUID(found["id"])
    return uuid.UUID(api.post("/routers", body)["id"])


def _ensure_group(api: Api, members: list[str]) -> None:
    found = _find(api.get_items("/model-groups"), name=GROUP_NAME)
    if found is None:
        api.post("/model-groups", {"name": GROUP_NAME, "members": members})
    else:
        api.patch(f"/model-groups/{found['id']}", {"members": members})


def _ensure_target(api: Api, path: str, body: dict[str, Any]) -> None:
    if _find(api.get_items(path), name=body["name"], zone=body["zone"]) is None:
        api.post(path, body)


def _ensure_key(
    api: Api,
    team_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    zone: str,
    secrets: dict[str, str],
    secret_key: str,
) -> uuid.UUID:
    found = _find(
        api.get_items(f"/teams/{team_id}/keys"),
        name=name,
        zone=zone,
        service_account_id=str(account_id),
    )
    if found:
        if not secrets.get(secret_key):
            # The secret is shown once; a lost file entry is recoverable only by rotation.
            rotated = api.post(f"/keys/{found['id']}/rotate", {})
            secrets[secret_key] = rotated["secret"]
        return uuid.UUID(found["id"])
    created = api.post(
        f"/teams/{team_id}/keys",
        {"name": name, "service_account_id": str(account_id), "zone": zone},
    )
    secrets[secret_key] = created["secret"]
    return uuid.UUID(created["key"]["id"])


# Business budgets are far above the seeded spend (roughly $0.5 per team per zone).
ORG_LIMITS = {"rpm_limit": 6000, "tpm_limit": 6_000_000, "max_parallel": 400, "budget": 100e9}
RESEARCH_ORG_LIMITS = {
    "rpm_limit": 3000,
    "tpm_limit": 3_000_000,
    "max_parallel": 200,
    "budget": 50e9,
}
TEAM_LIMITS = {"rpm_limit": 3000, "tpm_limit": 3_000_000, "max_parallel": 200, "budget": 40e9}
SA_LIMITS = {"rpm_limit": 2000, "tpm_limit": 2_000_000, "max_parallel": 100, "budget": 30e9}
MEMBER_LIMITS = {"rpm_limit": 1000, "tpm_limit": 1_000_000, "max_parallel": 50, "budget": 20e9}


def _limits_body(owner: dict[str, str], limits: dict[str, int]) -> dict[str, Any]:
    return {
        **owner,
        "rpm_limit": limits["rpm_limit"],
        "tpm_limit": limits["tpm_limit"],
        "max_parallel": limits["max_parallel"],
        "budget_nano_usd": int(limits["budget"]),
    }


def _ensure_limits(api: Api, zone: str, owner: dict[str, str], limits: dict[str, int]) -> None:
    api.put(f"/zones/{zone}/limits", _limits_body(owner, limits))


# --------------------------------------------------------------------------- catalog


def ensure_catalog(api: Api, secrets: dict[str, str]) -> SeedPlan:
    """Find-or-create every seed row; returns the ids the history generator needs.

    Memberships and org admins are skipped with a warning when the authz backend owns
    them (`DEV_AUTHZ=openfga` / `http`): the external IAM, not Postgres, is the source
    then, and the API refuses those writes.
    """
    org_ids = {name: _ensure_org(api, name) for name in ORGS}
    user_ids = {spec.email: _ensure_user(api, spec) for spec in USERS}

    # The grants name these targets, so they exist before the team policies are written.
    for zone in ZONES:
        _ensure_target(
            api,
            "/mcp-servers",
            {
                "name": "example",
                "zone": zone,
                "url": MCP_URL,
                "auth": {"plugin": "none", "params": {}},
            },
        )
        _ensure_target(
            api,
            "/a2a-agents",
            {
                "name": "helper",
                "zone": zone,
                "url": A2A_URL,
                "auth": {"plugin": "none", "params": {}},
            },
        )

    team_ids = {spec.name: _ensure_team(api, spec, org_ids[spec.org]) for spec in TEAMS}
    for zone in ZONES:
        _ensure_limits(api, zone, {"org_id": str(org_ids["acme"])}, ORG_LIMITS)
        _ensure_limits(api, zone, {"org_id": str(org_ids["research"])}, RESEARCH_ORG_LIMITS)

    # Names and targets must exist before a policy can grant them.
    model_ids = {spec.name: _ensure_model(api, spec) for spec in MODELS}
    upstreams = {zone: _ensure_upstream(api, zone) for zone in ZONES}
    for zone, upstream_id in upstreams.items():
        for spec in MODELS:
            api.put(
                f"/models/{model_ids[spec.name]}/deployments/{zone}",
                {"upstream_id": str(upstream_id), "upstream_model": spec.upstream_model},
            )

    chat_router = _ensure_router(
        api,
        {
            "name": "chat",
            "strategy": "least_in_flight",
            "members": [
                {"model_id": str(model_ids["llama-3.1-8b"])},
                {"model_id": str(model_ids["qwen2.5-32b"])},
            ],
            "fallback_model_ids": [str(model_ids[FALLBACK_MODEL])],
            "retry": {"max_attempts": 2},
            "session_affinity": True,
        },
    )
    # A router seeded before affinity existed gets it switched on as well.
    api.patch(f"/routers/{chat_router}", {"session_affinity": True})
    _ensure_router(
        api,
        {
            "name": "chat-ab",
            "strategy": "weighted",
            "traffic_split": [
                {
                    "name": "a",
                    "weight": 70,
                    "members": [{"model_id": str(model_ids["llama-3.1-8b"])}],
                },
                {
                    "name": "b",
                    "weight": 30,
                    "members": [{"model_id": str(model_ids["qwen2.5-32b"])}],
                },
            ],
        },
    )
    _ensure_alias(api, "chat-default", chat_router)
    _ensure_alias(api, "embed", model_ids["bge-m3"])
    _ensure_group(api, list(LLM_NAMES))

    for spec in TEAMS:
        _apply_team_policy(api, team_ids[spec.name], spec)
        for zone in ZONES:
            _ensure_limits(api, zone, {"team_id": str(team_ids[spec.name])}, TEAM_LIMITS)
    members_ok = True
    for spec in USERS:
        members_ok &= _add_member(api, team_ids[spec.team], user_ids[spec.email], spec.role)
    org_admin = _add_org_admin(api, org_ids["acme"], user_ids[ORG_ADMIN])
    if not members_ok or org_admin == "external":
        print(
            "seed-dev: memberships and org admins are managed by an external authz backend;"
            " users were created but not added",
            file=sys.stderr,
        )
    for zone in ZONES:
        for spec in USERS:
            _ensure_limits(
                api,
                zone,
                {"team_id": str(team_ids[spec.team]), "user_id": str(user_ids[spec.email])},
                MEMBER_LIMITS,
            )

    keys: list[SeedKey] = []
    for spec in TEAMS:
        account_id = _ensure_service_account(api, team_ids[spec.name], spec.service_account)
        for zone in ZONES:
            _ensure_limits(
                api,
                zone,
                {"team_id": str(team_ids[spec.name]), "service_account_id": str(account_id)},
                SA_LIMITS,
            )
            key_name = f"{spec.service_account}-{zone}"
            key_id = _ensure_key(
                api,
                team_ids[spec.name],
                account_id,
                key_name,
                zone,
                secrets,
                f"{spec.name}/{spec.service_account}/{zone}",
            )
            keys.append(
                SeedKey(
                    zone=zone,
                    team=spec.name,
                    team_id=team_ids[spec.name],
                    key_id=key_id,
                    owner_id=account_id,
                    service_account=spec.service_account,
                )
            )
    return SeedPlan(
        org_ids=org_ids,
        team_ids=team_ids,
        user_ids=user_ids,
        keys=keys,
        org_admin=org_admin,
    )


DEMO_KEYS = {"default": "demo", "prod": "demo-prod"}


def _demo_key_path(zone: str) -> Path:
    return _secrets_path().parent / ("demo-key" if zone == "default" else f"demo-key-{zone}")


def ensure_demo(api: Api, secrets: dict[str, str]) -> None:
    """The small demo the dev stack starts with: `mock-local` deployed in both zones, a
    router `demo` over it, an alias `demo-latest` to the router, and a team `demo` with one
    key per zone whose secret is in `.dev/demo-key` (default) and `.dev/demo-key-prod`."""
    org_id = _ensure_org(api, "default")
    api.patch(
        "/zones/prod",
        {"name": "Production", "color": "blue", "playground_gateway_url": "http://127.0.0.1:4002"},
    )
    found = _find(api.get_items("/models"), name="mock-local")
    model_id = (
        found["id"]
        if found
        else api.post("/models", {"name": "mock-local", "model_type": "llm"})["id"]
    )
    for zone in ZONES:
        api.put(
            f"/models/{model_id}/deployments/{zone}",
            {"upstream_id": str(_ensure_upstream(api, zone)), "upstream_model": "mock"},
        )
    router_id = _ensure_router(api, {"name": "demo", "members": [{"model_id": model_id}]})
    _ensure_alias(api, "demo-latest", router_id)
    found = _find(api.get_items("/teams"), name="demo", org_id=str(org_id))
    team_id = uuid.UUID(
        found["id"]
        if found
        else api.post("/teams", {"name": "demo", "org_id": str(org_id), "model_access": "all"})[
            "id"
        ]
    )
    account_id = _ensure_service_account(api, team_id, "chat")
    for zone, name in DEMO_KEYS.items():
        path = _demo_key_path(zone)
        secret_key = f"demo/chat/{zone}"
        if not secrets.get(secret_key) and path.exists():
            secrets[secret_key] = path.read_text().strip()
        _ensure_key(api, team_id, account_id, name, zone, secrets, secret_key)
        _write_private(path, secrets[secret_key] + "\n")


# --------------------------------------------------------------------------- history

HOUR_WEIGHTS = (
    0.8,
    0.5,
    0.4,
    0.4,
    0.5,
    0.7,
    1.0,
    1.6,
    3.0,
    5.5,
    7.0,
    7.0,
    5.0,
    6.0,
    7.0,
    6.0,
    5.0,
    4.0,
    3.0,
    2.5,
    2.0,
    1.6,
    1.2,
    0.9,
)
# Requests per key per UTC day; weekends are quieter.
DAY_REQUESTS = 58
WEEKEND_REQUESTS = 30


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _pick_timestamp(day: date, end: datetime, rng: random.Random) -> datetime:
    if day == end.date():
        start = datetime.combine(day, time(0), tzinfo=UTC)
        span = max(1, int((end - start).total_seconds()))
        return start + timedelta(seconds=rng.randrange(span))
    hour = rng.choices(range(24), weights=HOUR_WEIGHTS)[0]
    return datetime.combine(day, time(hour, rng.randrange(60), rng.randrange(60)), tzinfo=UTC)


def _duration(rng: random.Random, model: str, audio_ms: int = 0) -> int:
    if model == "whisper-large-v3":
        value = 200 + audio_ms * rng.uniform(0.3, 0.9)
    else:
        mu, sigma = {
            "llama-3.1-8b": (6.8, 0.35),
            "qwen2.5-32b": (7.0, 0.4),
            "gpt-4o-mini": (6.6, 0.3),
            "bge-m3": (3.4, 0.2),
        }.get(model, (6.7, 0.35))
        value = rng.lognormvariate(mu, sigma)
    return max(1, min(int(value), 2**31 - 1))


def _cost(spec: ModelSpec, prompt: int, completion: int, audio_ms: int) -> int:
    cost = 0
    if spec.price_in is not None:
        cost += prompt * spec.price_in // 1_000_000
    if spec.price_out is not None:
        cost += completion * spec.price_out // 1_000_000
    if spec.price_audio is not None and audio_ms:
        cost += audio_ms * spec.price_audio // 1000
    return cost


def _pick_llm(rng: random.Random) -> tuple[str, str, str, str]:
    """(requested, served model, alias, router) for one chat/completion request."""
    roll = rng.random()
    if roll < 0.30:
        served = rng.choices(("llama-3.1-8b", "qwen2.5-32b"), (50, 35))[0]
        return "chat", served, "", "chat"
    if roll < 0.50:
        served = rng.choices(("llama-3.1-8b", "qwen2.5-32b"), (50, 35))[0]
        return "chat-default", served, "chat-default", "chat"
    if roll < 0.65:
        served = rng.choices(("llama-3.1-8b", "qwen2.5-32b"), (70, 30))[0]
        return "chat-ab", served, "", "chat-ab"
    served = rng.choices(LLM_NAMES, (45, 30, 25))[0]
    return served, served, "", ""


def _llm_event(
    rng: random.Random,
    key: SeedKey,
    *,
    request_id: uuid.UUID,
    ts: datetime,
    endpoint: str,
) -> usage_pb2.UsageEvent:
    requested, served, alias, router = _pick_llm(rng)
    spec = MODEL_NAMES[served]
    stream = endpoint == "chat_completions" and rng.random() < 0.65
    failed = rng.random()
    fallback = False
    attempts = 1
    status = 200
    outcome = usage_pb2.REQUEST_OUTCOME_COMPLETED
    usage_state = usage_pb2.USAGE_STATE_REPORTED
    if failed < 0.015:
        status = 429
        outcome = usage_pb2.REQUEST_OUTCOME_REJECTED
        usage_state = usage_pb2.USAGE_STATE_MISSING
    elif failed < 0.03:
        status = 503
        outcome = usage_pb2.REQUEST_OUTCOME_UPSTREAM_ERROR
        usage_state = usage_pb2.USAGE_STATE_MISSING
        attempts = 2
    elif router and served != FALLBACK_MODEL and rng.random() < 0.10:
        # A member failed before any response byte; the router fell back.
        served = FALLBACK_MODEL
        spec = MODEL_NAMES[served]
        fallback = True
        attempts = 2

    if status != 200:
        prompt = completion = 0
    else:
        prompt = rng.randint(300, 1600)
        completion = rng.randint(80, 700)
    audio_ms = 0
    duration = _duration(rng, served) if status == 200 else rng.randint(5, 60)
    cost = 0
    cost_known = False
    if status == 200:
        cost = _cost(spec, prompt, completion, audio_ms)
        cost_known = True
    if usage_state is usage_pb2.USAGE_STATE_REPORTED and rng.random() < 0.02:
        # The upstream reported nothing usable: the request is real, the price is unknown.
        usage_state = usage_pb2.USAGE_STATE_INVALID
        cost = 0
        cost_known = False
    return usage_pb2.UsageEvent(
        request_id=str(request_id),
        timestamp_unix_ms=_ms(ts),
        started_at_unix_ms=_ms(ts - timedelta(milliseconds=duration)),
        key_id=str(key.key_id),
        team_id=str(key.team_id),
        requested=requested,
        model=served,
        alias=alias,
        router=router,
        upstream=f"mock-{key.zone}",
        upstream_model=spec.upstream_model,
        fallback=fallback,
        attempts=attempts,
        kind=usage_pb2.TRAFFIC_KIND_LLM,
        endpoint=endpoint,
        status=status,
        stream=stream,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_nano_usd=cost,
        cost_known=cost_known,
        duration_ms=duration,
        config_version=0,
        usage_state=usage_state,
        outcome=outcome,
    )


def _embedding_event(
    rng: random.Random, key: SeedKey, *, request_id: uuid.UUID, ts: datetime
) -> usage_pb2.UsageEvent:
    spec = MODEL_NAMES["bge-m3"]
    if rng.random() < 0.6:
        requested, alias = "embed", "embed"
    else:
        requested, alias = "bge-m3", ""
    prompt = rng.randint(50, 400)
    duration = _duration(rng, "bge-m3")
    return usage_pb2.UsageEvent(
        request_id=str(request_id),
        timestamp_unix_ms=_ms(ts),
        started_at_unix_ms=_ms(ts - timedelta(milliseconds=duration)),
        key_id=str(key.key_id),
        team_id=str(key.team_id),
        requested=requested,
        model="bge-m3",
        alias=alias,
        upstream=f"mock-{key.zone}",
        upstream_model=spec.upstream_model,
        attempts=1,
        kind=usage_pb2.TRAFFIC_KIND_LLM,
        endpoint="embeddings",
        status=200,
        prompt_tokens=prompt,
        completion_tokens=0,
        cost_nano_usd=_cost(spec, prompt, 0, 0),
        cost_known=True,
        duration_ms=duration,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )


def _transcription_event(
    rng: random.Random, key: SeedKey, *, request_id: uuid.UUID, ts: datetime
) -> usage_pb2.UsageEvent:
    spec = MODEL_NAMES["whisper-large-v3"]
    audio_ms = rng.randint(4000, 90_000)
    duration = _duration(rng, "whisper-large-v3", audio_ms)
    return usage_pb2.UsageEvent(
        request_id=str(request_id),
        timestamp_unix_ms=_ms(ts),
        started_at_unix_ms=_ms(ts - timedelta(milliseconds=duration)),
        key_id=str(key.key_id),
        team_id=str(key.team_id),
        requested="whisper-large-v3",
        model="whisper-large-v3",
        upstream=f"mock-{key.zone}",
        upstream_model=spec.upstream_model,
        attempts=1,
        kind=usage_pb2.TRAFFIC_KIND_LLM,
        endpoint="audio_transcriptions",
        status=200,
        audio_ms=audio_ms,
        cost_nano_usd=_cost(spec, 0, 0, audio_ms),
        cost_known=True,
        duration_ms=duration,
        usage_state=usage_pb2.USAGE_STATE_REPORTED,
        outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
    )


def _target_event(
    rng: random.Random, key: SeedKey, *, request_id: uuid.UUID, ts: datetime, kind: str
) -> usage_pb2.UsageEvent:
    failed = rng.random() < 0.03
    if kind == "mcp":
        target, method, tool = "example", "tools/call", rng.choice(("search", "summarize"))
        endpoint = "mcp"
    else:
        target, method, tool = "helper", "message/stream", ""
        endpoint = "a2a"
    duration = rng.randint(20, 400) if not failed else rng.randint(5, 60)
    return usage_pb2.UsageEvent(
        request_id=str(request_id),
        timestamp_unix_ms=_ms(ts),
        started_at_unix_ms=_ms(ts - timedelta(milliseconds=duration)),
        key_id=str(key.key_id),
        team_id=str(key.team_id),
        kind=usage_pb2.TRAFFIC_KIND_MCP if kind == "mcp" else usage_pb2.TRAFFIC_KIND_A2A,
        target=target,
        method=method,
        tool=tool,
        endpoint=endpoint,
        status=500 if failed else 200,
        duration_ms=duration,
        usage_state=(usage_pb2.USAGE_STATE_MISSING if failed else usage_pb2.USAGE_STATE_REPORTED),
        outcome=(
            usage_pb2.REQUEST_OUTCOME_UPSTREAM_ERROR
            if failed
            else usage_pb2.REQUEST_OUTCOME_COMPLETED
        ),
    )


def _one_event(
    rng: random.Random,
    key: SeedKey,
    *,
    request_id: uuid.UUID,
    ts: datetime,
) -> usage_pb2.UsageEvent:
    roll = rng.random()
    if roll < 0.68:
        return _llm_event(rng, key, request_id=request_id, ts=ts, endpoint="chat_completions")
    if roll < 0.71:
        return _llm_event(rng, key, request_id=request_id, ts=ts, endpoint="completions")
    if roll < 0.86:
        return _embedding_event(rng, key, request_id=request_id, ts=ts)
    if roll < 0.92:
        return _transcription_event(rng, key, request_id=request_id, ts=ts)
    if roll < 0.96:
        return _target_event(rng, key, request_id=request_id, ts=ts, kind="mcp")
    return _target_event(rng, key, request_id=request_id, ts=ts, kind="a2a")


def generate_events(
    keys: Sequence[SeedKey], *, seed: int, days: int, end: datetime
) -> dict[str, list[usage_pb2.UsageEvent]]:
    """A deterministic window of `days` UTC days ending now, keyed by zone.

    The request ids are `uuid5` over (seed, zone, team, day, slot), so two runs on the
    same database replay the same rows: a delete-then-ingest is idempotent.
    """
    end = end.astimezone(UTC).replace(microsecond=0)
    rng = random.Random(seed)
    events: dict[str, list[usage_pb2.UsageEvent]] = defaultdict(list)
    for key in keys:
        for offset in range(max(days, 0)):
            day = (end - timedelta(days=offset)).date()
            base = DAY_REQUESTS if day.weekday() < 5 else WEEKEND_REQUESTS
            count = int(base * rng.uniform(0.85, 1.15))
            if offset == 0:
                start = datetime.combine(day, time(0), tzinfo=UTC)
                count = int(count * max(0.0, (end - start).total_seconds() / 86400))
            for slot in range(count):
                ts = _pick_timestamp(day, end, rng)
                request_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"lumewarp-seed-dev:{seed}:{key.zone}:{key.team}"
                    f":{key.service_account}:{day.isoformat()}:{slot}",
                )
                events[key.zone].append(_one_event(rng, key, request_id=request_id, ts=ts))
    return dict(events)


# --------------------------------------------------------------------------- totals


async def _seeded_scope_ids(session: AsyncSession, team_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Team, member and service-account scope ids whose totals belong to the seed teams."""
    scopes: set[uuid.UUID] = set(team_ids)
    rows = (
        await session.execute(
            select(TeamMember.team_id, TeamMember.user_id).where(TeamMember.team_id.in_(team_ids))
        )
    ).all()
    scopes.update(member_scope_id(team_id, user_id) for team_id, user_id in rows)
    scopes.update(
        (
            await session.scalars(
                select(ServiceAccount.id).where(ServiceAccount.team_id.in_(team_ids))
            )
        ).all()
    )
    return scopes


async def _backfill_catalog_ids(session: AsyncSession, team_ids: set[uuid.UUID]) -> None:
    """Attribute the backdated events to the catalog items by their current name.

    `resolve_catalog_ids` binds a name to an id only for events at or after that item's
    creation, which no historical event is. The seed teams' rows are ours, so naming the
    current owners directly keeps renames, `called` and the per-model aggregates coherent.
    A name that is not in the catalog (a rejection with no model) keeps its id NULL.
    """
    if not team_ids:
        return
    for kind, column in (("model", "model"), ("alias", "alias"), ("router", "router")):
        await session.execute(
            text(
                f"UPDATE usage_events AS e SET {column}_id = n.id"
                " FROM catalog_names AS n"
                f" WHERE n.name = e.{column} AND n.kind = :kind"
                f" AND e.{column} <> '' AND e.team_id IN :teams"
            ).bindparams(bindparam("teams", expanding=True)),
            {"kind": kind, "teams": sorted(team_ids)},
        )


async def _rebuild_totals(session: AsyncSession, team_ids: set[uuid.UUID]) -> int:
    """Replace the seed scopes' `usage_totals` rows with sums of their remaining events."""
    scope_ids = await _seeded_scope_ids(session, team_ids)
    await session.execute(delete(UsageTotal).where(UsageTotal.scope_id.in_(scope_ids)))
    rows = (
        await session.execute(
            select(
                UsageEvent.zone,
                UsageEvent.ts,
                UsageEvent.team_id,
                UsageEvent.owner_kind,
                UsageEvent.owner_id,
                UsageEvent.cost_nano_usd,
                UsageEvent.prompt_tokens,
                UsageEvent.completion_tokens,
                UsageEvent.audio_ms,
                UsageEvent.cost_known,
            ).where(
                UsageEvent.team_id.in_(team_ids),
                UsageEvent.kind == TrafficKind.LLM,
            )
        )
    ).all()
    totals: dict[tuple[str, ScopeType, uuid.UUID, datetime], list[int]] = {}
    for zone, ts, team_id, owner_kind, owner_id, cost, prompt, completion, audio, known in rows:
        month = month_window(ts)[0]
        owner_scope = (
            ScopeType.MEMBER if owner_kind is KeyOwnerKind.USER else ScopeType.SERVICE_ACCOUNT
        )
        # A member's budget row is keyed per team (`member_scope_id`), as `usage.ingest` writes it.
        owner_scope_id = (
            member_scope_id(team_id, owner_id) if owner_kind is KeyOwnerKind.USER else owner_id
        )
        for kind, scope_id in ((ScopeType.TEAM, team_id), (owner_scope, owner_scope_id)):
            bucket = totals.setdefault((zone, kind, scope_id, month), [0, 0, 0, 0, 0, 0])
            for index, amount in enumerate((cost, prompt, completion, 1, int(not known), audio)):
                bucket[index] += amount
    rows_written = 0
    for (zone, kind, scope_id, month), amounts in totals.items():
        session.add(
            UsageTotal(
                zone=zone,
                scope_type=kind,
                scope_id=scope_id,
                window_start=month,
                cost_nano_usd=amounts[0],
                prompt_tokens=amounts[1],
                completion_tokens=amounts[2],
                request_count=amounts[3],
                unknown_cost_count=amounts[4],
                audio_ms=amounts[5],
            )
        )
        rows_written += 1
    await session.flush()
    return rows_written


async def _write_history(
    settings: Settings,
    plan: SeedPlan,
    events: dict[str, list[usage_pb2.UsageEvent]],
    *,
    chunk: int = 2000,
) -> tuple[int, Counter[str]]:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    team_ids = set(plan.team_ids.values())
    accepted = 0
    skipped: Counter[str] = Counter()
    try:
        async with factory() as session, session.begin():
            await session.execute(delete(UsageEvent).where(UsageEvent.team_id.in_(team_ids)))
        for zone, batch in events.items():
            for start in range(0, len(batch), chunk):
                async with factory() as session, session.begin():
                    accepted += await usage.ingest(
                        session,
                        f"seed-dev-{zone}",
                        batch[start : start + chunk],
                        zone=zone,
                        skipped=skipped,
                    )
        async with factory() as session, session.begin():
            await _backfill_catalog_ids(session, team_ids)
            await _rebuild_totals(session, team_ids)
    finally:
        await engine.dispose()
    return accepted, skipped


# --------------------------------------------------------------------------- secrets


def _secrets_path() -> Path:
    repo = Path(__file__).resolve().parents[3]
    return repo / ".dev" / "seed-keys"


def _read_secrets(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    # Created 0600: the secrets are never readable by others, not even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    tmp.replace(path)


def _write_secrets(path: Path, secrets: dict[str, str]) -> None:
    _write_private(path, json.dumps(secrets, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- entry


def run(settings: Settings, args: Any) -> None:
    _guard(settings)
    password = settings.break_glass_password.get_secret_value()
    url = args.url or DEFAULT_URL
    # The break-glass password is sent there: only to this machine.
    if not _loopback(httpx.URL(url).host):
        raise SeedRefused(f"refusing a non-loopback control plane URL ({url})")
    api = Api(url, password)
    path = _secrets_path()
    secrets = _read_secrets(path)
    try:
        ensure_demo(api, secrets)
        plan = ensure_catalog(api, secrets)
    finally:
        api.close()
        # Written even on a failed run: a key whose secret was just created must not be lost.
        _write_secrets(path, secrets)
    if plan.org_admin == "direct":
        asyncio.run(_write_org_admin(settings, plan.org_ids["acme"], plan.user_ids[ORG_ADMIN]))
    if args.no_usage:
        print(f"seed-dev: catalog ready ({len(plan.keys)} keys); usage skipped")
        return

    events = generate_events(plan.keys, seed=args.seed, days=args.days, end=datetime.now(UTC))
    count = sum(len(batch) for batch in events.values())
    accepted, skipped = asyncio.run(_write_history(settings, plan, events))
    print(
        f"seed-dev: catalog ready, {accepted}/{count} usage events over {args.days} days"
        f" (seed {args.seed}), secrets in {path}"
        + (f"; skipped {dict(skipped)}" if skipped else "")
    )
