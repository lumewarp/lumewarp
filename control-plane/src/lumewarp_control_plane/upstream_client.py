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
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from lumewarp_control_plane import sealing
from lumewarp_control_plane.models import Upstream
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
    max_input_tokens: int | None
    max_output_tokens: int | None


# Where OpenAI-compatible servers report limits (OpenAI itself reports none): vLLM
# `max_model_len`; OpenRouter, Together, Groq, LiteLLM and others use the rest.
_INPUT_FIELDS = ("max_model_len", "context_length", "context_window", "max_input_tokens")
_OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens", "max_tokens")


def _positive(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**31:
        return value
    return None


def _first(item: dict, fields: tuple[str, ...]) -> int | None:
    for field in fields:
        if (value := _positive(item.get(field))) is not None:
            return value
    return None


def model_limits(item: dict) -> tuple[int | None, int | None]:
    """(input, output) from one `/models` entry. Without a reported output limit the
    context window is used: no completion can be longer.
    """
    top = item.get("top_provider")
    nested = top if isinstance(top, dict) else {}
    window = _first(item, _INPUT_FIELDS) or _first(nested, ("context_length",))
    output = _first(item, _OUTPUT_FIELDS) or _first(nested, ("max_completion_tokens",))
    return window, output or window


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
        out.setdefault(item["id"], UpstreamModelInfo(item["id"], *model_limits(item)))
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


CHAT_CHECK_PROMPT = "1 + 1 = ?"
MAX_REPLY_CHARS = 200


async def chat_check(
    base_url: str,
    token: str | None,
    upstream_model: str,
    default_body: dict | None,
    model_type: str = "llm",
) -> tuple[ProbeResult, str | None]:
    """One type-specific probe matching the gateway's periodic check. JSON probes
    place default body below their own fields; transcription uploads generated silence.
    Returns the probe and a chat reply excerpt; errors never carry the response body.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        **(default_body or {}),
        "model": upstream_model,
        "messages": [{"role": "user", "content": CHAT_CHECK_PROMPT}],
        "max_tokens": 16,
        "stream": False,
    }
    if model_type == "embedding":
        url = f"{base_url.rstrip('/')}/embeddings"
        body = {**(default_body or {}), "model": upstream_model, "input": "ping"}
    elif model_type == "decision":
        url = f"{base_url.rstrip('/')}/decisions"
        body = {
            **(default_body or {}),
            "model": upstream_model,
            "input": "ping",
            "questions": [
                {"type": "predicate", "name": "ok", "instructions": "Is the input a word?"}
            ],
        }
    elif model_type == "transcription":
        url = f"{base_url.rstrip('/')}/audio/transcriptions"
    started = time.monotonic()
    try:
        async with _client() as client:
            if model_type == "transcription":
                wav = struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF",
                    32036,
                    b"WAVE",
                    b"fmt ",
                    16,
                    1,
                    1,
                    16000,
                    32000,
                    2,
                    16,
                    b"data",
                    32000,
                ) + bytes(32000)
                async with client.stream(
                    "POST",
                    url,
                    headers=_headers(token),
                    data={"model": upstream_model, "response_format": "json"},
                    files={"file": ("probe.wav", wav, "audio/wav")},
                ) as response:
                    out = bytearray()
                    async for chunk in response.aiter_bytes():
                        out += chunk
                        if len(out) > MAX_BODY_BYTES:
                            raise UpstreamCallFailed("response larger than 4 MiB")
                    status, raw = response.status_code, bytes(out)
            else:
                status, raw = await _post(client, url, token, body)
    except (httpx.HTTPError, UpstreamCallFailed) as exc:
        message = _describe(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        return ProbeResult(url, None, _elapsed(started), False, message), None
    ok = 200 <= status < 300
    return ProbeResult(url, status, _elapsed(started), ok, None), _reply(raw) if ok else None


def _reply(raw: bytes) -> str | None:
    try:
        choice = json.loads(raw)["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or message.get("reasoning_content") or choice.get("text")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    return text.strip()[:MAX_REPLY_CHARS] if isinstance(text, str) else None


async def _post(
    client: httpx.AsyncClient, url: str, token: str | None, body: dict
) -> tuple[int, bytes]:
    async with client.stream("POST", url, headers=_headers(token), json=body) as response:
        out = bytearray()
        async for chunk in response.aiter_bytes():
            out += chunk
            if len(out) > MAX_BODY_BYTES:
                raise UpstreamCallFailed("response larger than 4 MiB")
        return response.status_code, bytes(out)


def health_url(row: Upstream) -> str:
    """The URL the gateway probes (router.rs): explicit, or base_url + /models."""
    return default_health_url(row.base_url, row.health_url)


def default_health_url(base_url: str, explicit: str | None) -> str:
    """Explicit, or base_url with its path preserved plus /models (every kind)."""
    if explicit:
        return explicit
    return f"{base_url.rstrip('/')}/models"


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    port = {"http": 80, "https": 443}
    return (pa.scheme, pa.hostname, pa.port or port.get(pa.scheme)) == (
        pb.scheme,
        pb.hostname,
        pb.port or port.get(pb.scheme),
    )


async def test_upstream(base_url: str, health_url: str | None, token: str | None) -> ProbeResult:
    """Probe one health URL, as the gateway would."""
    url = default_health_url(base_url, health_url)
    return await probe(url, token if _same_origin(url, base_url) else None)
