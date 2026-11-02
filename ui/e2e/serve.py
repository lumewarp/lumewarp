"""Start the control plane against a fresh, migrated Postgres database for the Playwright smoke test.

Run via `uv run --project ../control-plane python e2e/serve.py` (see playwright.config.ts). Creates
`lumewarp_test_e2e_<hex>` on the dev Postgres (or LUMEWARP_TEST_DATABASE_URL), migrates and
bootstraps it, seeds a viewer user with a known cookie session, serves until signalled, then
drops the database.
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
VIEWER_EMAIL = "viewer@e2e.test"
VIEWER_TOKEN = "e2e-viewer-session-0123456789abcdef"
SERVER_URL = os.environ.get("LUMEWARP_TEST_DATABASE_URL", DEV_DATABASE_URL)


async def admin(sql: str) -> None:
    engine = create_async_engine(SERVER_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def seed_viewer(url: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            user = User(email=VIEWER_EMAIL, display_name="E2E Viewer")
            session.add(user)
            await session.flush()
            session.add(
                UserSession(
                    user_id=user.id,
                    token_hash=hash_secret(VIEWER_TOKEN),
                    csrf_hash=hash_secret(csrf_token(VIEWER_TOKEN)),
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

# What gateways report in Hello for the strategies the tests use (docs/plans/model-catalog.md, "Schema 子集").
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
    """A catalog with traffic through every kind of path (docs/plans/model-catalog.md, "Usage 記錄"):
    direct, via an alias, via a router, via an alias to a router, and mixed groups.
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

    now = datetime.now(UTC)
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
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            for requested, name, via_alias, via_router, tokens, count in events:
                for n in range(count):
                    session.add(
                        UsageEvent(
                            request_id=uuid.uuid4(),
                            gateway_id="e2e-gw",
                            ts=now - timedelta(minutes=5 + n),
                            started_at=now - timedelta(minutes=5 + n, seconds=1),
                            key_id=key,
                            team_id=team,
                            owner_kind=KeyOwnerKind.USER,
                            owner_id=owner,
                            requested=requested,
                            model=name,
                            alias=via_alias["name"] if via_alias else "",
                            router=via_router["name"] if via_router else "",
                            upstream="use-up",
                            upstream_model=model[name]["upstream_model"],
                            fallback=False,
                            attempts=1,
                            model_id=uuid.UUID(model[name]["id"]),
                            alias_id=uuid.UUID(via_alias["id"]) if via_alias else None,
                            router_id=uuid.UUID(via_router["id"]) if via_router else None,
                            endpoint="chat.completions",
                            status=200,
                            stream=False,
                            prompt_tokens=tokens,
                            completion_tokens=tokens // 2,
                            cost_nano_usd=tokens * 1000,
                            cost_known=True,
                            duration_ms=120,
                            config_version=1,
                            usage_state=UsageState.REPORTED,
                            outcome=RequestOutcome.COMPLETED,
                        )
                    )
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
        asyncio.run(seed_viewer(url))
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
