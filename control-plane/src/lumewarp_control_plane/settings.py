"""Process settings, read from `LUMEWARP_*` environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    # Optional TLS for the gateway-facing gRPC listener. Without it the
    # listener is plaintext and must sit behind a trusted boundary.
    grpc_tls_cert_file: str | None = None
    grpc_tls_key_file: str | None = None

    # Installation-level superuser credential (see security.py). Unset = no
    # bootstrap access; PATs only.
    bootstrap_token: SecretStr | None = None
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
    budget_flip_deadline_seconds: float = 5.0

    # Run the gRPC server and background loops in `serve`. Tests turn it off.
    background_enabled: bool = True

    @field_validator("http_bind", "grpc_bind")
    @classmethod
    def _check_bind(cls, v: str) -> str:
        split_bind(v)
        return v

    @property
    def http_host_port(self) -> tuple[str, int]:
        return split_bind(self.http_bind)


@lru_cache
def get_settings() -> Settings:
    return Settings()
