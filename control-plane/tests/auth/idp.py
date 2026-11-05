"""An in-process RSA OIDC provider; no network connections are made."""

import base64
import hashlib
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://idp.example/realms/test"


class FakeIdP:
    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "test"})
        self.claims = {
            "iss": ISSUER,
            "sub": str(uuid.uuid4()),
            "aud": "lumewarp",
            "email": f"{uuid.uuid4().hex}@example.com",
            "email_verified": True,
            "name": "OIDC User",
        }
        self.overrides = {}
        self.authorization = {}
        # Per-login state for concurrent flows. `authorization` stays the latest, so the
        # single-flow tests keep working with the unregistered "test-code".
        self.by_code = {}
        self.last_state = None
        self.last_code = None
        self.requests = []
        self.token_error = False
        self.signing_key = self.key
        self.userinfo = {"sub": self.claims["sub"]}

    def handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/jwks",
                    "userinfo_endpoint": ISSUER + "/userinfo",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(200, json=self.userinfo)
        if request.url.path.endswith("/token"):
            if self.token_error:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["authorization_code"]
            code = body.get("code", ["test-code"])[0]
            authorization = self.by_code.get(code, self.authorization)
            assert body["redirect_uri"] == authorization["redirect_uri"]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert challenge == authorization["code_challenge"][0]
            now = int(time.time())
            claims = {
                **self.claims,
                "iat": now,
                "exp": now + 300,
                "nonce": authorization["nonce"][0],
                **self.overrides,
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "unused-access-token",
                    "token_type": "Bearer",
                    "id_token": jwt.encode(
                        {"alg": "RS256", "kid": "test"}, claims, self.signing_key
                    ),
                },
            )
        raise AssertionError(f"unexpected IdP endpoint: {request.url.path}")

    def login(self, client, next="/"):
        response = client.get(
            "/api/v1/session/oidc/login", params={"next": next}, follow_redirects=False
        )
        assert response.status_code == 302, response.text
        self.authorization = parse_qs(urlsplit(response.headers["location"]).query)
        assert "state" in self.authorization, response.headers["location"]
        self.last_state = self.authorization["state"][0]
        self.last_code = f"test-code-{uuid.uuid4().hex}"
        self.by_code[self.last_code] = self.authorization
        return response

    def callback(self, client, **params):
        return client.get(
            "/api/v1/session/oidc/callback",
            params={"state": self.authorization["state"][0], "code": "test-code", **params},
            follow_redirects=False,
        )
