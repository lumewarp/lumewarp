"""Process settings, read from `LUMEWARP_*` environment variables."""

from functools import lru_cache
from ipaddress import ip_network
from typing import Annotated

from pydantic import Field, IPvAnyNetwork, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_DATABASE_URL = "postgresql+asyncpg://lumewarp:lumewarp@127.0.0.1:55432/lumewarp"

MIB = 1024 * 1024


def split_bind(bind: str) -> tuple[str, int]:
    """Split `host:port` (IPv6 hosts in brackets) into host and port."""
    host, sep, port = bind.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {bind!r}")
    return host.strip("[]"), int(port)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUMEWARP_", extra="ignore")

    database_url: str = DEV_DATABASE_URL
    db_pool_size: int = Field(default=10, ge=1)

    http_bind: str = "127.0.0.1:8091"
    grpc_bind: str = "127.0.0.1:50051"
    # Prometheus metrics listener (text format, any path), separate from the admin API.
    # Empty string disables it. Not 9464: that is the gateway admin listener's default, and
    # both often run on one dev host.
    metrics_bind: str = "127.0.0.1:9465"
    # Optional TLS for the gateway-facing gRPC listener. Without it the
    # listener is plaintext and must sit behind a trusted boundary.
    grpc_tls_cert_file: str | None = None
    grpc_tls_key_file: str | None = None

    # The email that becomes proxy_admin on its first verified login while no proxy_admin
    # exists. Unset = only break-glass can create the first one.
    initial_admin_email: str | None = None
    break_glass_password_hash: SecretStr | None = None
    break_glass_password: SecretStr | None = None
    dev_mode: bool = False
    break_glass_session_ttl_seconds: int = Field(default=3600, ge=60, le=14400)
    trusted_proxies: Annotated[list[IPvAnyNetwork], NoDecode] = Field(default_factory=list)
    break_glass_allowed_cidrs: Annotated[list[IPvAnyNetwork], NoDecode] = Field(
        default_factory=list
    )
    # Authorization backend: "builtin" or a `lumewarp.authz` entry point name (authz/).
    authz_backend: str = "builtin"
    # Every authorization backend call is bounded; a timeout is Decision.ERROR (503).
    authz_timeout_seconds: float = Field(default=2.0, gt=0)
    # `openfga` backend (authz/openfga.py). dev: scripts/dev-idp.sh up prints these.
    openfga_url: str | None = None
    openfga_store_id: str | None = None
    openfga_model_id: str | None = None
    openfga_api_token: SecretStr | None = None
    # Attach an existing store whose names differ from Lumewarp's. Both maps are shared by
    # Check, BatchCheck, ListObjects and ReadChanges; unset = today's names.
    #   LUMEWARP_OPENFGA_OBJECT_TYPES='{"team": "llm_team", "org": "llm_org"}'
    #   LUMEWARP_OPENFGA_RELATIONS='{"team.admin": "owner"}'
    openfga_object_types: dict[str, str] = Field(default_factory=dict)
    openfga_relations: dict[str, str] = Field(default_factory=dict)
    # `http` backend (authz/http.py): POST {user_id, relation, resource} -> {decision}.
    authz_http_url: str | None = None
    authz_http_token: SecretStr | None = None

    # Key reconciler (keys/reconciler.py): suspends keys whose owner lost team membership.
    # Polls the backend's changes() this often when it has one; otherwise rescans every
    # key each full interval. A full rescan also runs every full interval as a safety net.
    key_reconcile_poll_seconds: float = Field(default=2.0, gt=0)
    key_reconcile_full_seconds: float = Field(default=300.0, gt=0)
    key_reconcile_enabled: bool = True

    # Admin UI login with OIDC (authorization code + PKCE, auth/oidc.py). Unset issuer =
    # SSO disabled (break-glass sessions and PATs only).
    #
    # `secret_key` seals the transient OIDC login cookie (nonce + PKCE verifier) with
    # AES-256-GCM; it is required whenever SSO is enabled and never leaves the process.
    secret_key: SecretStr | None = Field(default=None, min_length=32)
    # Seals upstream API keys stored in the database (sealing.py). Every gateway gets the
    # same value to open them; changing it makes stored keys unreadable until re-entered.
    # Separate from `secret_key`, which never leaves the control plane.
    encryption_key: SecretStr | None = Field(default=None, min_length=32)
    # Gateway client listener for the session-authenticated Playground proxy; unset disables it.
    playground_gateway_url: str | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: str = "openid email profile"
    # Externally visible base URL of the control plane, used to build the OIDC redirect
    # URI (<public_url>/api/v1/session/oidc/callback). Unset = derived from the request.
    public_url: str | None = None
    # Create a user with no grants on first SSO login. Off = only pre-created users
    # (matched by verified email on first login) may sign in.
    oidc_jit_users: bool = True
    session_ttl_seconds: int = Field(default=12 * 3600, ge=60)
    # `Secure` on the session cookie. Turn off only for plain-http dev on a non-localhost.
    session_cookie_secure: bool = True
    # Config scope this control plane publishes (Snapshot.scope).
    scope: str = "default"

    # Limits shared with the gateway, see docs/config-protocol.md.
    max_snapshot_bytes: int = 64 * MIB
    max_snapshot_keys: int = 200_000
    grpc_max_message_bytes: int = 64 * MIB
    ack_timeout_seconds: float = 30.0
    config_versions_retained: int = 50
    # Usage ingestion keeps every key/request id IN-list under asyncpg's 32767-parameter cap.
    max_usage_batch_events: int = Field(default=10_000, ge=1, le=10_000)

    # Run the gRPC server and background loops in `serve`. Tests turn it off.
    background_enabled: bool = True

    @model_validator(mode="after")
    def _sso_needs_secret_key(self) -> "Settings":
        if self.oidc_issuer and not self.secret_key:
            raise ValueError("LUMEWARP_SECRET_KEY is required when LUMEWARP_OIDC_ISSUER is set")
        return self

    @field_validator("trusted_proxies", "break_glass_allowed_cidrs", mode="before")
    @classmethod
    def _cidrs(cls, value):
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        return [ip_network(item) for item in value]

    @field_validator("http_bind", "grpc_bind")
    @classmethod
    def _check_bind(cls, v: str) -> str:
        split_bind(v)
        return v

    @field_validator("metrics_bind")
    @classmethod
    def _check_metrics_bind(cls, v: str) -> str:
        if v:
            split_bind(v)
        return v

    @property
    def http_host_port(self) -> tuple[str, int]:
        return split_bind(self.http_bind)


@lru_cache
def get_settings() -> Settings:
    return Settings()
