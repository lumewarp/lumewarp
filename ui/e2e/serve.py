"""Start the control plane against a fresh, migrated Postgres database for the Playwright smoke test.

Run via `uv run --project ../control-plane python e2e/serve.py` (see playwright.config.ts). Creates
`lumewarp_test_e2e_<hex>` on the dev Postgres (or LUMEWARP_TEST_DATABASE_URL), migrates and
bootstraps it, seeds the users the UI specs sign in as (each with a known cookie session), serves
until signalled, then drops the database.
"""

import asyncio
import os
import signal
import subprocess
import sys
import http.cookiejar
import json
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

from lumewarp_control_plane.auth.sessions import csrf_token
from lumewarp_control_plane.models import (
    KeyOwnerKind,
    RequestOutcome,
    UsageEvent,
    UsageState,
    GatewaySession,
    GatewayStrategy,
    GatewayToken,
    User,
    UserSession,
)
from lumewarp_control_plane.security import hash_secret
from lumewarp_control_plane.settings import DEV_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

HTTP_PORT = os.environ.get("E2E_HTTP_PORT", "8992")
GRPC_PORT = os.environ.get("E2E_GRPC_PORT", "50952")
BREAK_GLASS_PASSWORD = "e2e-break-glass-password"
# Users without a team: a spec adds them where it needs them. Their sessions are seeded here because the
# login is OIDC (or break-glass); a cookie session is the only way a spec can act as a regular member.
VIEWER_EMAIL = "viewer@e2e.test"
VIEWER_TOKEN = "e2e-viewer-session-0123456789abcdef"
MEMBER_EMAIL = "member@e2e.test"
MEMBER_TOKEN = "e2e-member-session-0123456789abcdef"
OUTSIDER_EMAIL = "outsider@e2e.test"
OUTSIDER_TOKEN = "e2e-outsider-session-0123456789abcdef"
TEAM_ADMIN_EMAIL = "teamadmin@e2e.test"
TEAM_ADMIN_TOKEN = "e2e-teamadmin-session-0123456789abcdef"
SHOTS_EMAIL = "shots@e2e.test"
SHOTS_TOKEN = "e2e-shots-session-0123456789abcdef"
SERVER_URL = os.environ.get("LUMEWARP_TEST_DATABASE_URL", DEV_DATABASE_URL)


async def admin(sql: str) -> None:
    engine = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def seed_users(url: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            # One org for the specs' teams: the Admin UI has no Orgs page yet, and a team
            # needs an explicit org.
            from lumewarp_control_plane.models import Org

            session.add(Org(name="e2e"))
            for email, display_name, token in (
                (VIEWER_EMAIL, "E2E Viewer", VIEWER_TOKEN),
                (MEMBER_EMAIL, "E2E Member", MEMBER_TOKEN),
                (OUTSIDER_EMAIL, "E2E Outsider", OUTSIDER_TOKEN),
                (TEAM_ADMIN_EMAIL, "E2E Team Admin", TEAM_ADMIN_TOKEN),
                (SHOTS_EMAIL, "E2E Shots", SHOTS_TOKEN),
            ):
                user = User(email=email, display_name=display_name)
                session.add(user)
                await session.flush()
                session.add(
                    UserSession(
                        user_id=user.id,
                        token_hash=hash_secret(token),
                        csrf_hash=hash_secret(csrf_token(token)),
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
    finally:
        await engine.dispose()


BUILTINS = ["weighted", "least_in_flight", "lowest_latency", "vllm_load"]
HOSTILE = (
    '<img src=x onerror="window.__pwned=1"> <b>bold</b> '
    "[click](javascript:window.__pwned=1) https://evil.example/phish"
)

# What gateways report in Hello for the strategies the tests use (docs/router-strategies.md, "Schema subset").
# `priority_failover` is the reference extension strategy: its descriptor is the one its crate declares.
STRATEGY_DESCRIPTORS: dict[str, dict] = {
    "priority_failover": {
        "title": "Priority failover",
        "description": "Use the lowest priority number that is healthy.",
        "uses_weight": False,
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_in_flight": {
                    "type": "integer",
                    "minimum": 1,
                    "title": "Max in flight",
                    "description": "Skip members with more requests in flight than this.",
                }
            },
        },
        "member_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["priority"],
            "properties": {
                "priority": {
                    "type": "integer",
                    "minimum": 1,
                    "title": "Priority",
                    "description": "Lower numbers are used first.",
                }
            },
        },
    },
    # Params and member params of every kind of field, supported by both gateways.
    "e2e_tuned": {
        "title": "E2E tuned",
        "description": "Exercises every field kind of the schema subset.",
        "uses_weight": False,
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_waiting"],
            "properties": {
                "headers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "x-lumewarp-widget": "header-list",
                    "title": "Pin headers",
                    "description": "Requests with the same value go to the same member.",
                },
                "max_waiting": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "title": "Max waiting requests",
                    "description": "Skip members with a longer queue.",
                },
                "mode": {"type": "string", "enum": ["strict", "relaxed"], "title": "Mode"},
                # Choices of every shape the Select contract has to carry: a 64 entry enum (the schema subset's
                # limit), number enums where 0 is a value and "" as a legal value. Optional, so the other tests
                # that use this strategy never see them in a request.
                "big": {"type": "string", "enum": [f"opt-{i:02d}" for i in range(64)], "title": "Big list"},
                "level": {"type": "integer", "enum": [0, 1, 5], "title": "Level"},
                "ratio": {"type": "number", "enum": [0.5, 1, 2], "title": "Ratio"},
                "scope": {"type": "string", "enum": ["", "wide", "narrow"], "title": "Scope"},
                "skip_unpriced": {"type": "boolean", "title": "Skip unpriced models"},
                "tuning": {
                    "type": "object",
                    "additionalProperties": False,
                    "title": "Tuning",
                    "properties": {"fail_back_ms": {"type": "integer", "minimum": 0, "title": "Fail back after (ms)"}},
                },
            },
        },
        "member_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"region": {"type": "string", "title": "Region"}},
        },
    },
    # Text a hostile gateway could report: it must only ever be shown as text.
    "e2e_hostile": {
        "title": HOSTILE,
        "description": HOSTILE + " " + "x" * 3000,
        "uses_weight": True,
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "title": HOSTILE,
            "description": HOSTILE,
            "properties": {
                "danger": {
                    "type": "string",
                    "title": HOSTILE,
                    "description": HOSTILE,
                },
                "pick": {"type": "string", "enum": [HOSTILE, "safe"], "title": "Pick"},
            },
        },
        "member_schema": {"type": "object", "additionalProperties": False},
    },
    # Presence rules and property names: required fields of every kind (a required boolean, an enum whose
    # value "" is legal, a required list and object), an optional object with a required field of its own,
    # and property names that are also members of Object.prototype or `__proto__` itself.
    "e2e_presence": {
        "title": "E2E presence",
        "description": "Required and optional fields; empty values are values.",
        "uses_weight": False,
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["enabled", "scope", "regions", "limits"],
            "properties": {
                "enabled": {"type": "boolean", "title": "Enabled"},
                "scope": {"type": "string", "enum": ["", "wide"], "title": "Scope"},
                "regions": {"type": "array", "items": {"type": "string"}, "title": "Regions"},
                "limits": {
                    "type": "object",
                    "additionalProperties": False,
                    "title": "Limits",
                    "properties": {"soft": {"type": "integer", "title": "Soft limit"}},
                },
                "prefix": {"type": "string", "title": "Prefix", "default": "pre-"},
                "tuning": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["priority"],
                    "title": "Tuning",
                    "properties": {
                        "priority": {"type": "integer", "minimum": 1, "title": "Priority"},
                        "note": {"type": "string", "title": "Note"},
                    },
                },
                "constructor": {"type": "string", "title": "Constructor"},
                "toString": {"type": "string", "title": "To string"},
                "__proto__": {
                    "type": "object",
                    "additionalProperties": False,
                    "title": "Proto",
                    "properties": {"inner": {"type": "string", "title": "Inner"}},
                },
            },
        },
        "member_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"region": {"type": "string", "title": "Region"}},
        },
    },
    # Two strategies without a usable schema, to switch between.
    "e2e_schemaless2": {
        "title": "E2E schemaless two",
        "description": "Also reported without a schema.",
        "uses_weight": True,
        "params_schema": None,
        "member_schema": None,
        "schema_problems": [],
    },
    # No usable schema (null) and one outside the subset (kept null with a problem): JSON editor.
    "e2e_schemaless": {
        "title": "E2E schemaless",
        "description": "Reported without a schema the control plane could use.",
        "uses_weight": True,
        "params_schema": None,
        "member_schema": None,
        "schema_problems": ["$: keyword 'oneOf' is outside the subset"],
    },
}
# Not on gateway B: saving them needs "Save anyway".
ONLY_ON_A = {"priority_failover"}


async def seed_gateways(url: str) -> None:
    """Two connected gateways. Done after the server is up: serving marks every stored gateway
    disconnected, and only connected gateways make a strategy "missing"."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    now = datetime.now(UTC)
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            token_a = await session.scalar(select(GatewayToken).where(GatewayToken.gateway_id == "e2e-gw"))
            assert token_a is not None
            token_b = GatewayToken(
                scope=token_a.scope,
                gateway_id="e2e-gw-b",
                name="b",
                token_hash=uuid.uuid4().bytes * 2,
                display_prefix="lwg-e2eb",
            )
            session.add(token_b)
            await session.flush()
            for token in (token_a, token_b):
                names = [n for n in STRATEGY_DESCRIPTORS if token is token_a or n not in ONLY_ON_A]
                session.add(
                    GatewaySession(
                        scope=token.scope,
                        gateway_id=token.gateway_id,
                        token_id=token.id,
                        connected=True,
                        gateway_version="e2e",
                        capabilities=[f"strategy:{n}" for n in [*BUILTINS, *names]],
                        connected_at=now,
                        last_seen_at=now,
                    )
                )
            await session.flush()
            for token in (token_a, token_b):
                for name in [n for n in STRATEGY_DESCRIPTORS if token is token_a or n not in ONLY_ON_A]:
                    session.add(
                        GatewayStrategy(
                            scope=token.scope,
                            gateway_id=token.gateway_id,
                            name=name,
                            digest="e2e" + name.replace("_", "")[:20],
                            descriptor={
                                "name": name,
                                "needs": [],
                                "schema_problems": [],
                                **STRATEGY_DESCRIPTORS[name],
                            },
                            token_id=token.id,
                            seen_at=now,
                        )
                    )
    finally:
        await engine.dispose()


class Api:
    """The control plane's HTTP API as the break-glass admin, for fixtures that need real catalog ids."""

    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""
        self.call("POST", "/session/break-glass", {"password": BREAK_GLASS_PASSWORD})
        self.csrf = self.call("GET", "/session")["csrf_token"]

    def call(self, method: str, path: str, data: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{HTTP_PORT}/api/v1{path}",
            method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={"content-type": "application/json", "x-lumewarp-csrf": self.csrf},
        )
        with self.opener.open(request, timeout=10) as response:
            body = response.read()
        return json.loads(body) if body else {}


async def seed_usage(url: str) -> None:
    """A catalog with traffic through every kind of path:
    direct, via an alias, via a router, via an alias to a router, and mixed groups.

    Plus a trend across UTC days for the charts: a day with no usage (day -3), a model whose requests
    have no price, a model priced only partly, and nine models on one days-ago slot so the chart has to
    fold the tail into "Other".
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    api = Api()
    upstream = api.call("POST", "/upstreams", {"name": "use-up", "kind": "vllm", "base_url": "http://127.0.0.1:9/v1"})
    model = {
        name: api.call("POST", "/models", {"name": name, "upstream_id": upstream["id"], "upstream_model": up_model})
        for name, up_model in [("use/a", "Qwen/UseA"), ("use/b", "Qwen/UseA"), ("use/c", "Other/UseC")]
    }
    # Existed once: usage keeps its names, the catalog does not.
    model["use/retired"] = {"id": str(uuid.uuid4()), "upstream_model": "Org/Retired"}
    router = api.call(
        "POST", "/routers", {"name": "use-router", "members": [{"model_id": model["use/a"]["id"]}, {"model_id": model["use/b"]["id"]}]}
    )
    alias = api.call("POST", "/aliases", {"name": "use-alias", "target": router["id"]})
    direct_alias = api.call("POST", "/aliases", {"name": "use-direct", "target": model["use/c"]["id"]})

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    owner, team, key = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        # (requested, model, alias, router, tokens, count)
        ("use-alias", "use/a", alias, router, 1000, 2),
        ("use-alias", "use/b", alias, router, 500, 1),
        ("use/c", "use/c", None, None, 200, 1),
        # Served under an upstream name no model maps to any more (the model was remapped or deleted).
        ("use/retired", "use/retired", None, None, 100, 1),
        ("use-direct", "use/c", direct_alias, None, 300, 1),
    ]
    # (days_ago, model, tokens, cost known?). Day -3 stays empty: the chart draws an empty slot for it.
    trend = [
        (0, "trend/unpriced", 900, False),
        (0, "trend/partial", 300, False),
        (1, "trend/a", 4000, True),
        (1, "trend/b", 1000, True),
        (1, "trend/partial", 500, True),
        (2, "trend/a", 2500, True),
        (4, "trend/c", 1200, True),
        (5, "trend/a", 800, True),
        # A name long enough to wrap in the tooltip.
        (8, "trend/a-very-long-model-name-for-the-tooltip", 40, True),
        # Nine models, strictly decreasing, on one day outside the default 30-day window: 8 hues + Other.
        *[(40, f"fold/m{n}", 1000 - n * 100, True) for n in range(1, 10)],
        # A subset of the same models a day later: a range change must not repaint the survivors.
        (39, "fold/m1", 700, True),
        (39, "fold/m3", 500, True),
        (39, "fold/m9", 300, True),
    ]
    # (days_ago, model, tokens, cost_nano_usd): m10 is last by tokens and first by cost, so the token rank and
    # the cost rank give different top eights. A chart that ranks by the drawn metric then names other models.
    rank = [(50, f"rank/m{n}", 1000 - n * 100, True, (1000 - n * 100) * 1000) for n in range(1, 10)]
    rank.append((50, "rank/m10", 10, True, 900_000_000))
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            def add(ts: datetime, requested: str, name: str, via_alias: dict | None, via_router: dict | None, tokens: int, known: bool, cost: int | None = None) -> None:
                session.add(
                    UsageEvent(
                        request_id=uuid.uuid4(),
                        gateway_id="e2e-gw",
                        ts=ts,
                        started_at=ts - timedelta(seconds=1),
                        key_id=key,
                        team_id=team,
                        owner_kind=KeyOwnerKind.USER,
                        owner_id=owner,
                        requested=requested,
                        model=name,
                        alias=via_alias["name"] if via_alias else "",
                        router=via_router["name"] if via_router else "",
                        upstream="use-up",
                        upstream_model=model[name]["upstream_model"] if name in model else "Trend/Model",
                        fallback=False,
                        attempts=1,
                        model_id=uuid.UUID(model[name]["id"]) if name in model else None,
                        alias_id=uuid.UUID(via_alias["id"]) if via_alias else None,
                        router_id=uuid.UUID(via_router["id"]) if via_router else None,
                        endpoint="chat.completions",
                        status=200,
                        stream=False,
                        prompt_tokens=tokens,
                        completion_tokens=tokens // 2,
                        cost_nano_usd=cost if cost is not None else (tokens * 1000 if known else 0),
                        cost_known=known,
                        duration_ms=120,
                        config_version=1,
                        usage_state=UsageState.REPORTED,
                        outcome=RequestOutcome.COMPLETED,
                    )
                )

            for requested, name, via_alias, via_router, tokens, count in events:
                for n in range(count):
                    add(now - timedelta(minutes=5 + n), requested, name, via_alias, via_router, tokens, True)
            for days_ago, name, tokens, known, *explicit in trend + rank:
                # Noon UTC, so a run at midnight cannot land two events on one day or split a bucket.
                cost = explicit[0] if explicit else None
                add((now - timedelta(days=days_ago)).replace(hour=12), name, name, None, None, tokens, known, cost)
    finally:
        await engine.dispose()


def wait_ready() -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/readyz", timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("control plane did not become ready")


def cp(*args: str, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "lumewarp_control_plane", *args], env=env, check=True
    )


def main() -> None:
    name = f"lumewarp_test_e2e_{uuid.uuid4().hex[:12]}"
    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    url = make_url(SERVER_URL).set(database=name).render_as_string(hide_password=False)
    env = {
        **os.environ,
        "LUMEWARP_DATABASE_URL": url,
        "LUMEWARP_BREAK_GLASS_PASSWORD": BREAK_GLASS_PASSWORD,
        "LUMEWARP_DEV_MODE": "true",
        "LUMEWARP_SESSION_COOKIE_SECURE": "false",
        "LUMEWARP_METRICS_BIND": "",
        "LUMEWARP_HTTP_BIND": f"127.0.0.1:{HTTP_PORT}",
        "LUMEWARP_GRPC_BIND": f"127.0.0.1:{GRPC_PORT}",
    }
    server: subprocess.Popen[bytes] | None = None
    try:
        cp("migrate", env=env)
        cp("bootstrap", "--gateway-id", "e2e-gw", env=env)
        asyncio.run(seed_users(url))
        server = subprocess.Popen(
            [sys.executable, "-m", "lumewarp_control_plane", "serve"], env=env
        )
        wait_ready()
        asyncio.run(seed_gateways(url))
        asyncio.run(seed_usage(url))
        signal.signal(signal.SIGTERM, lambda *_: server and server.terminate())
        signal.signal(signal.SIGINT, lambda *_: server and server.terminate())
        server.wait()
    finally:
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


if __name__ == "__main__":
    main()
