"""Admin-initiated calls from the control plane to an upstream: list its models
(`GET <base_url>/models`, OpenAI list format) and probe it once on demand.

These run only when an administrator asks (Admin UI), never on the gateway request path.
The bearer token is the stored sealed key or, for `api_key_ref`, the variable of that name
in the control plane's own environment when it has one. Upstream response bodies are never
copied into errors: they can echo credentials.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from lumewarp_control_plane import sealing
from lumewarp_control_plane.models import Upstream, UpstreamKind
from lumewarp_control_plane.settings import Settings

TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_MODELS = 2000

# Tests replace this with an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None


class UpstreamCallFailed(Exception):
    """The upstream could not be reached or answered with something unusable."""


@dataclass(frozen=True)
class UpstreamModelInfo:
    id: str
    # vLLM's context window (`max_model_len`); OpenAI does not report one.
    max_model_len: int | None


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status: int | None
    latency_ms: int
    ok: bool
    error: str | None


def bearer(settings: Settings, row: Upstream) -> str | None:
    if row.api_key_ciphertext is not None:
        try:
            return sealing.unseal(settings, row.api_key_ciphertext)
        except sealing.SealingUnavailable as exc:
            raise UpstreamCallFailed(
                "LUMEWARP_ENCRYPTION_KEY is not configured on the control plane"
            ) from exc
        except sealing.SealedInvalid as exc:
            raise UpstreamCallFailed(
                "the stored API key does not open with LUMEWARP_ENCRYPTION_KEY"
            ) from exc
    if row.api_key_ref:
        return os.environ.get(row.api_key_ref) or None
    return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport)


def _headers(token: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"} if token else {}


async def _get(client: httpx.AsyncClient, url: str, token: str | None) -> tuple[int, bytes]:
    async with client.stream("GET", url, headers=_headers(token)) as response:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                raise UpstreamCallFailed("response larger than 4 MiB")
        return response.status_code, bytes(body)


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out after {TIMEOUT_SECONDS:g} s"
    if isinstance(exc, httpx.ConnectError):
        return "connection failed"
    return type(exc).__name__


async def list_models(settings: Settings, row: Upstream) -> list[UpstreamModelInfo]:
    token = bearer(settings, row)
    url = f"{row.base_url.rstrip('/')}/models"
    try:
        async with _client() as client:
            status, body = await _get(client, url, token)
    except httpx.HTTPError as exc:
        raise UpstreamCallFailed(f"GET {url}: {_describe(exc)}") from None
    if status != 200:
        raise UpstreamCallFailed(f"GET {url} returned HTTP {status}")
    try:
        data = json.loads(body).get("data")
    except (ValueError, AttributeError):
        data = None
    if not isinstance(data, list):
        raise UpstreamCallFailed(f"GET {url} did not return an OpenAI model list")
    out: dict[str, UpstreamModelInfo] = {}
    for item in data[:MAX_MODELS]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        length = item.get("max_model_len")
        valid = isinstance(length, int) and not isinstance(length, bool) and length > 0
        out.setdefault(item["id"], UpstreamModelInfo(item["id"], length if valid else None))
    return sorted(out.values(), key=lambda m: m.id)


async def probe(url: str, token: str | None) -> ProbeResult:
    """One GET; 2xx within the timeout is healthy, like the gateway's active probe."""
    started = time.monotonic()
    try:
        async with _client() as client:
            status, _ = await _get(client, url, token)
    except (httpx.HTTPError, UpstreamCallFailed) as exc:
        message = _describe(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        return ProbeResult(url, None, _elapsed(started), False, message)
    return ProbeResult(url, status, _elapsed(started), 200 <= status < 300, None)


def _elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def health_url(row: Upstream) -> str:
    """The URL the gateway probes (router.rs): explicit, or the default per kind."""
    if row.health_check_url:
        return row.health_check_url
    base = row.base_url.rstrip("/")
    if row.kind == UpstreamKind.VLLM:
        return f"{base.removesuffix('/v1')}/health"
    return f"{base}/models"


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    port = {"http": 80, "https": 443}
    return (pa.scheme, pa.hostname, pa.port or port.get(pa.scheme)) == (
        pb.scheme,
        pb.hostname,
        pb.port or port.get(pb.scheme),
    )


async def test_upstream(settings: Settings, row: Upstream) -> tuple[ProbeResult, ProbeResult]:
    """Probe the health URL and `/models` once each, as the gateway would."""
    token = bearer(settings, row)
    health = health_url(row)
    models = f"{row.base_url.rstrip('/')}/models"
    return (
        await probe(health, token if _same_origin(health, row.base_url) else None),
        await probe(models, token),
    )
