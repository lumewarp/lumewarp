"""Real dev identity services, with isolated grants and reversible realm changes."""

import json
import subprocess
import threading
import uuid
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import SecretStr
from stack import ROOT, Stack, record_response_secrets, record_secret

KEYCLOAK = "http://127.0.0.1:18080"
OPENFGA = "http://127.0.0.1:18081"
ADMIN_GROUP = "group:lumewarp-admins"


def cookie_api(browser, stack, method, path, **kwargs):
    """A cookie-session request the test expects to succeed; returns its JSON body.

    Any credential the response carries is recorded before this returns, so the tests that
    create or rotate keys through a browser session do not have to remember it for the
    pg_dump scan.
    """
    response = browser.request(method, stack.api_url + path, **kwargs)
    record_response_secrets(response, f"{method.upper()} {path}")
    assert response.status_code in (200, 201, 204), (
        method,
        path,
        response.status_code,
        response.text,
    )
    return response.json() if response.content else None


class LoginForm(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.action = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs["action"]


def _record_login_secrets(browser, stack, location):
    """Record the OIDC state and what the sealed login-state cookie carries.

    The nonce and PKCE verifier exist only inside the cookie (the database keeps a hash of
    the state), so decrypting it here is the only way the scan can look for them.
    """
    from lumewarp_control_plane.auth.oidc import (
        STATE_COOKIE,
        LoginStateInvalid,
        open_login_state,
    )

    for state in parse_qs(urlparse(location).query).get("state", []):
        record_secret(state, "OIDC state")
    sealed = browser.cookies.get(STATE_COOKIE)
    if not sealed or not stack.cp_env.get("LUMEWARP_SECRET_KEY"):
        return
    settings = SimpleNamespace(secret_key=SecretStr(stack.cp_env["LUMEWARP_SECRET_KEY"]))
    try:
        login_state = open_login_state(settings, sealed)
    except LoginStateInvalid:
        return
    record_secret(login_state.state, "OIDC state")
    record_secret(login_state.nonce, "OIDC nonce")
    record_secret(login_state.code_verifier, "OIDC PKCE code_verifier")


def login(browser, stack, username, *, enabled=True):
    response = browser.get(stack.api_url + "/session/oidc/login")
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(KEYCLOAK + "/realms/lumewarp/")
    assert "code_challenge_method=S256" in location
    _record_login_secrets(browser, stack, location)
    response = browser.get(location, follow_redirects=True)
    assert response.status_code == 200, response.text
    # Keycloak marks its restart cookie Secure even in start-dev. Browsers treat
    # loopback HTTP as trustworthy; Python's CookieJar does not. Apply that local
    # exception only to the real dev IdP's cookies, leaving RP cookies unchanged.
    for cookie in browser.cookies.jar:
        if cookie.domain == "127.0.0.1" and cookie.path.startswith("/realms/lumewarp"):
            cookie.secure = False
    action = LoginForm(response.text).action
    assert action and action.startswith(KEYCLOAK + "/"), response.text
    response = browser.post(
        action,
        data={"username": username, "password": "lumewarp", "credentialId": ""},
    )
    if not enabled:
        assert response.status_code == 200, response.text
        assert LoginForm(response.text).action, "disabled user should remain at the login form"
        assert not browser.cookies.get("lumewarp_session")
        assert browser.get(stack.api_url + "/session").json()["authenticated"] is False
        return None
    # Follow only the authorization redirects, stopping before the SPA return path.
    for _ in range(10):
        assert response.status_code in (302, 303), response.text
        target = str(response.url.join(response.headers["location"]))
        assert target.startswith((KEYCLOAK + "/", stack.api_url + "/session/oidc/callback"))
        if urlparse(target).path == "/api/v1/session/oidc/callback":
            for code in parse_qs(urlparse(target).query).get("code", []):
                record_secret(code, "OIDC authorization code")
        response = browser.get(target)
        if urlparse(target).path == "/api/v1/session/oidc/callback":
            break
    else:
        raise AssertionError("OIDC callback was not reached")
    assert response.status_code == 302 and response.headers["location"] == "/", response.text
    from lumewarp_control_plane.auth.sessions import COOKIE_NAME

    assert browser.cookies.get(COOKIE_NAME)
    session = browser.get(stack.api_url + "/session").json()
    assert session["authenticated"] and session["csrf_token"]
    record_secret(browser.cookies.get(COOKIE_NAME), "Lumewarp session cookie")
    record_response_secrets(session, "GET /session")
    browser.headers["X-Lumewarp-CSRF"] = session["csrf_token"]
    return session["user"]


@contextmanager
def identity_stack(backend: str, *, label: str = "identity"):
    """Real Keycloak + an isolated OpenFGA store + a control plane configured for OIDC.

    Yields a namespace with the stack, the IdP, Bob's and Alice's cookie sessions and (for
    openfga) the store and fault proxy. Alice is a member of the configured admin group in
    the backend; Bob has no grants. Callers use `state.cleanup` (an ExitStack) for anything
    they add, and create the team(s) and grants their tests need.
    """
    stack = Stack()
    with ExitStack() as cleanup:
        cleanup.callback(stack.cleanup_dir)
        cleanup.callback(stack.teardown)
        idp = KeycloakAdmin()
        cleanup.callback(idp.http.close)
        cleanup.enter_context(idp.redirect(stack.api_url))
        stack.cp_env.update(
            {
                "LUMEWARP_AUTHZ_BACKEND": backend,
                "LUMEWARP_OIDC_ISSUER": "http://127.0.0.1:18080/realms/lumewarp",
                "LUMEWARP_OIDC_CLIENT_ID": "lumewarp-cp",
                "LUMEWARP_OIDC_CLIENT_SECRET": "lumewarp-dev-secret",
                "LUMEWARP_SESSION_COOKIE_SECURE": "false",
                "LUMEWARP_PUBLIC_URL": f"http://127.0.0.1:{stack.http_port}",
                "LUMEWARP_KEY_RECONCILE_POLL_SECONDS": "1",
                "LUMEWARP_KEY_RECONCILE_FULL_SECONDS": "1",
            }
        )
        state = SimpleNamespace(stack=stack, backend=backend, idp=idp, cleanup=cleanup)
        if backend == "openfga":
            state.store, model = cleanup.enter_context(fga_store())
            state.proxy = FGAProxy()
            cleanup.callback(state.proxy.close)
            stack.cp_env.update(
                {
                    "LUMEWARP_OPENFGA_URL": state.proxy.url,
                    "LUMEWARP_OPENFGA_STORE_ID": state.store,
                    "LUMEWARP_OPENFGA_MODEL_ID": model,
                }
            )
        try:
            stack.setup()
            state.member = cleanup.enter_context(httpx.Client(timeout=15, trust_env=False))
            state.admin = cleanup.enter_context(httpx.Client(timeout=15, trust_env=False))
            state.user = login(state.member, stack, "bob@example.com")
            state.admin_user = login(state.admin, stack, "alice@example.com")
            if backend == "openfga":
                fga_write(
                    state,
                    writes=[
                        {
                            "user": f"user:{state.admin_user['id']}",
                            "relation": "member",
                            "object": ADMIN_GROUP,
                        }
                    ],
                )
            assert cookie_api(state.admin, stack, "GET", "/users/me")["is_admin"]
            yield state
        finally:
            # Preserve useful diagnostics even when a prerequisite fixture fails.
            print(f"\n===== {label} {backend} process diagnostics =====\n{stack.logs()}")


def fga_write(state, *, writes=(), deletes=()) -> None:
    body = {}
    if writes:
        body["writes"] = {"tuple_keys": list(writes)}
    if deletes:
        body["deletes"] = {"tuple_keys": list(deletes)}
    if not body:
        return
    response = httpx.post(
        f"{OPENFGA}/stores/{state.store}/write", json=body, timeout=10, trust_env=False
    )
    response.raise_for_status()


def grant(state, user, role, *, delete=False):
    """Give (or remove) a team relation through the backend that owns memberships."""
    team_id = state.team["id"] if isinstance(state.team, dict) else state.team
    if state.backend == "builtin":
        method = "DELETE" if delete else "PUT"
        kwargs = {} if delete else {"json": {"role": role}}
        response = state.stack.api(method, f"/teams/{team_id}/members/{user['id']}", **kwargs)
        assert response.status_code in (200, 201, 204), response.text
    else:
        key = {"user": f"user:{user['id']}", "relation": role, "object": f"team:{team_id}"}
        fga_write(state, deletes=[key] if delete else [], writes=[] if delete else [key])


class KeycloakAdmin:
    def __init__(self):
        self.http = httpx.Client(timeout=15, trust_env=False)
        response = self.http.post(
            KEYCLOAK + "/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": "admin",
                "password": "lumewarp",
            },
        )
        response.raise_for_status()
        self.http.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        self.base = KEYCLOAK + "/admin/realms/lumewarp"
        response = self.http.get(self.base + "/clients", params={"clientId": "lumewarp-cp"})
        response.raise_for_status()
        self.client_url = self.base + "/clients/" + response.json()[0]["id"]

    @contextmanager
    def redirect(self, api_url):
        uri = api_url + "/session/oidc/callback"

        def update(add):
            response = self.http.get(self.client_url)
            response.raise_for_status()
            client = response.json()
            uris = client.get("redirectUris", [])
            client["redirectUris"] = (
                list(dict.fromkeys([*uris, uri])) if add else [item for item in uris if item != uri]
            )
            self.http.put(self.client_url, json=client).raise_for_status()

        update(True)
        try:
            yield
        finally:
            update(False)

    @contextmanager
    def temporary_user(self):
        username = f"e2e-{uuid.uuid4().hex}@example.com"
        response = self.http.post(
            self.base + "/users",
            json={
                "username": username,
                "email": username,
                "emailVerified": True,
                "enabled": True,
                "firstName": "E2E",
                "lastName": "Temporary",
                "requiredActions": [],
                "credentials": [{"type": "password", "value": "lumewarp", "temporary": False}],
            },
        )
        response.raise_for_status()
        user_url = response.headers["location"]
        try:
            yield username, user_url
        finally:
            self.http.delete(user_url).raise_for_status()


class FGAProxy:
    """Fault only this test's traffic; count completed reconciler requests."""

    def __init__(self):
        self.failed = False
        self.condition = threading.Condition()
        self.failures = {"changes": 0, "batch-check": 0}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with proxy.condition:
                    failed = proxy.failed
                # A closed loopback port simulates unreachability without stopping shared IAM.
                url = ("http://127.0.0.1:1" if failed else OPENFGA) + self.path
                try:
                    with httpx.Client(timeout=2, trust_env=False) as client:
                        response = client.request(
                            self.command,
                            url,
                            content=body,
                            headers={"Content-Type": "application/json"},
                        )
                    status, content = response.status_code, response.content
                except httpx.TransportError:
                    status, content = 503, b'{"error":"test IAM unavailable"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                if failed:
                    endpoint = urlparse(self.path).path.rsplit("/", 1)[-1]
                    with proxy.condition:
                        if endpoint in proxy.failures:
                            proxy.failures[endpoint] += 1
                        proxy.condition.notify_all()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def outage(self):
        with self.condition:
            self.failed = True
            self.failures = dict.fromkeys(self.failures, 0)

    def wait_polls(self):
        with self.condition:
            assert self.condition.wait_for(lambda: min(self.failures.values()) >= 2, timeout=15), (
                f"did not observe two failed change polls and full scans: {self.failures}"
            )
        print(f"OpenFGA outage: observed failed reconciler calls {self.failures}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextmanager
def fga_store():
    process = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ROOT / 'deploy/openfga/model.fga'}:/model.fga:ro",
            "openfga/cli:v0.8.2",
            "model",
            "transform",
            "--file",
            "/model.fga",
            "--output-format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    with httpx.Client(base_url=OPENFGA, timeout=10, trust_env=False) as client:
        response = client.post("/stores", json={"name": f"e2e-identity-{uuid.uuid4()}"})
        response.raise_for_status()
        store = response.json()["id"]
        try:
            response = client.post(
                f"/stores/{store}/authorization-models", json=json.loads(process.stdout)
            )
            response.raise_for_status()
            yield store, response.json()["authorization_model_id"]
        finally:
            client.delete(f"/stores/{store}").raise_for_status()
