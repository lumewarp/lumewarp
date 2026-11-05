"""Regression evidence for the security review."""

import asyncio
import logging
from ipaddress import ip_network
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from starlette.requests import Request

from lumewarp_control_plane.api import session as session_api
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.auth import break_glass, oidc
from lumewarp_control_plane.authz import Authorizer, Decision
from lumewarp_control_plane.models import AuditLog, UserIdentity
from lumewarp_control_plane.settings import Settings
from tests.api.helpers import auth, user_with_token
from tests.api.test_session import idp as idp


def peer_request(peer, forwarded):
    return Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        }
    )


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("192.0.2.1", "198.51.100.1", "192.0.2.1"),
        ("10.0.0.1", "198.51.100.1, 10.0.0.2", "198.51.100.1"),
        ("10.0.0.1", "198.51.100.1, 192.0.2.2", "192.0.2.2"),
        ("10.0.0.1", "not-an-ip", "10.0.0.1"),
    ],
)
def test_forwarding_uses_only_trusted_proxy_chain(peer, forwarded, expected):
    settings = Settings(trusted_proxies="10.0.0.0/8")
    assert break_glass.client_ip(peer_request(peer, forwarded), settings) == expected
    assert break_glass.client_ip(peer_request(peer, forwarded), Settings()) == peer


def test_ip_table_has_a_hard_bound_and_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(break_glass, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    limiter = break_glass.LoginLimiter()
    for index in range(break_glass.MAX_IPS + 20):
        assert limiter.reserve(str(index))
    assert len(limiter.attempts) == break_glass.MAX_IPS
    clock[0] += break_glass.WINDOW_SECONDS
    assert limiter.reserve("fresh")
    assert len(limiter.attempts) == 1


def test_duplicate_forwarded_headers_preserve_rightmost_untrusted_rule():
    request = peer_request("10.0.0.1", "198.51.100.1")
    request.scope["headers"].append((b"x-forwarded-for", b"192.0.2.2"))
    assert break_glass.client_ip(request, Settings(trusted_proxies="10.0.0.0/8")) == "192.0.2.2"


def test_flood_cannot_lock_out_other_client(api_settings, monkeypatch):
    api_settings.trusted_proxies = [ip_network("10.0.0.0/8")]
    delays = []

    async def delay(seconds):
        delays.append(seconds)

    monkeypatch.setattr(
        session_api, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread, sleep=delay)
    )
    app = create_app(api_settings)
    with TestClient(app, client=("10.0.0.1", 1234)) as browser:
        for _ in range(5):
            assert (
                browser.post(
                    "/api/v1/session/break-glass",
                    headers={"X-Forwarded-For": "192.0.2.1"},
                    json={"password": "wrong"},
                ).status_code
                == 401
            )
        for _ in range(20):
            assert (
                browser.post(
                    "/api/v1/session/break-glass",
                    headers={"X-Forwarded-For": "192.0.2.1"},
                    json={"password": "wrong"},
                ).status_code
                == 429
            )
        app.state.break_glass_limiter.failures = 100
        response = browser.post(
            "/api/v1/session/break-glass",
            headers={"X-Forwarded-For": "192.0.2.2"},
            json={"password": "test-break-glass-password"},
        )
        assert response.status_code == 204
        assert delays == [break_glass.MAX_GLOBAL_DELAY_SECONDS]


async def test_rate_limited_attempts_write_at_most_one_summary_per_window(
    client, committed, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(break_glass, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    count = (
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "session.break_glass.login")
    )
    before = await committed.scalar(count)
    for _ in range(5):
        assert (
            client.post("/api/v1/session/break-glass", json={"password": "wrong"}).status_code
            == 401
        )
    for _ in range(30):
        assert (
            client.post("/api/v1/session/break-glass", json={"password": "wrong"}).status_code
            == 429
        )
    assert await committed.scalar(count) == before + 6
    assert (
        client.app.state.metrics.registry.get_sample_value(
            "lumewarp_cp_break_glass_logins_total", {"result": "rate_limited"}
        )
        == 30
    )
    clock[0] += break_glass.WINDOW_SECONDS
    for _ in range(6):
        client.post("/api/v1/session/break-glass", json={"password": "wrong"})
    assert await committed.scalar(count) == before + 12


def test_optional_client_network_restriction_is_not_an_authentication_bypass(client):
    client.app.state.settings.break_glass_allowed_cidrs = [ip_network("192.0.2.0/24")]
    assert (
        client.post(
            "/api/v1/session/break-glass", json={"password": "test-break-glass-password"}
        ).status_code
        == 404
    )
    client.app.state.settings.break_glass_allowed_cidrs = []
    assert client.post("/api/v1/session/break-glass", json={"password": "wrong"}).status_code == 401


@pytest.mark.parametrize("change", ["disable", "plaintext", "hash"])
def test_emergency_sessions_are_bound_to_current_credential(client, change):
    if change == "hash":
        client.app.state.settings.break_glass_password_hash = SecretStr(
            PasswordHasher().hash("test-break-glass-password")
        )
    assert (
        client.post(
            "/api/v1/session/break-glass", json={"password": "test-break-glass-password"}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/users/me").status_code == 200
    settings = client.app.state.settings
    if change == "disable":
        settings.break_glass_password = None
    elif change == "plaintext":
        settings.break_glass_password = SecretStr("new-password")
    else:
        settings.break_glass_password_hash = SecretStr(PasswordHasher().hash("new-password"))
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/session").json()["authenticated"] is False


@pytest.mark.parametrize(
    "decision,allowed", [(Decision.ALLOW, False), (Decision.ERROR, False), (Decision.DENY, True)]
)
async def test_external_admin_email_link_is_refused(
    client, committed, idp, monkeypatch, decision, allowed
):
    user, _ = await user_with_token(committed, email=idp.claims["email"])
    await committed.commit()
    calls = []

    async def check(uid, relation, resource):
        calls.append((uid, relation, resource.type, resource.id))
        return decision

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(check, "external"))
    idp.login(client)
    response = idp.callback(client)
    assert response.headers["location"] == ("/" if allowed else "/login?error=access_denied")
    assert calls == [(str(user.id), "member", "group", "lumewarp-admins")]
    linked = await committed.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id))
    assert (linked is not None) is allowed


@pytest.mark.parametrize(
    "claim,value",
    [
        ("groups", "lumewarp-admins"),
        ("groups", "/lumewarp-admins"),
        ("groups", ["/lumewarp-admins", "unrelated-group"]),
        ("realm_access.roles", ["/lumewarp-admins"]),
    ],
)
def test_group_claim_shapes_and_dotted_paths(client, idp, claim, value):
    client.app.state.settings.oidc_groups_claim = claim
    if "." in claim:
        idp.claims["realm_access"] = {"roles": value}
    else:
        idp.claims[claim] = value
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/"
    assert client.get("/api/v1/users/me").json()["is_admin"]


@pytest.mark.parametrize("value", [None, 123, {"wrong": "shape"}, ["admin", 1]])
def test_unusable_claim_is_logged_without_contents(client, caplog, value):
    request = Request({"type": "http", "app": client.app})
    with caplog.at_level(logging.DEBUG, logger=oidc.__name__):
        assert oidc.groups({"groups": value}, request) == ()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert caplog.records[0].args == ()


@pytest.mark.parametrize("failure", ["subject", "error"])
def test_userinfo_failure_does_not_create_session(client, idp, monkeypatch, failure):
    idp.claims.pop("groups")
    if failure == "subject":
        idp.userinfo["sub"] = "other-user"
    else:

        async def unavailable(**kwargs):
            raise RuntimeError("userinfo unavailable")

        monkeypatch.setattr(client.app.state.oidc_client, "userinfo", unavailable)
    idp.login(client)
    assert idp.callback(client).headers["location"] == "/login?error=oidc_error"
    assert not client.get("/api/v1/session").json()["authenticated"]


@pytest.mark.parametrize(
    "encoded",
    [
        "not-a-hash",
        "$argon2id$bad",
        PasswordHasher(type=Type.I).hash("password"),
    ],
)
def test_startup_rejects_invalid_password_hash(encoded):
    with pytest.raises(ValueError):
        create_app(Settings(break_glass_password_hash=encoded, background_enabled=False))


def test_plaintext_requires_development_mode_and_session_ttl_is_capped():
    with pytest.raises(ValueError):
        create_app(Settings(break_glass_password="password", background_enabled=False))
    with pytest.raises(ValidationError):
        Settings(break_glass_session_ttl_seconds=14401)
    assert Settings(break_glass_session_ttl_seconds=14400).break_glass_session_ttl_seconds == 14400


async def test_identity_shell_survives_iam_outage_but_admin_actions_fail(
    client, committed, monkeypatch
):
    _, token = await user_with_token(committed)
    await committed.commit()

    async def unavailable(*args):
        return Decision.ERROR

    monkeypatch.setattr(client.app.state, "authorizer", Authorizer(unavailable, "external"))
    for path in ("/session", "/users/me"):
        response = client.get("/api/v1" + path, headers=auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["authz_unavailable"]
        assert not body.get("user", body)["is_admin"]
    assert client.get("/api/v1/users", headers=auth(token)).status_code == 503
