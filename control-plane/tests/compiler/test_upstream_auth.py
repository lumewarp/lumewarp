"""LLM upstreams can take a WASM upstream-auth plugin; plain ones stay byte-identical."""

from __future__ import annotations

import json

import pytest

from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler.compile import compile_scope
from lumewarp_control_plane.compiler.validate import SnapshotInvalid, validate_snapshot
from lumewarp_control_plane.db import SessionFactory
from tests import factories
from tests.compiler.helpers import SCOPE, seed_scope


async def compile_(factory: SessionFactory):
    async with factory() as s:
        return await compile_scope(s, SCOPE, require_local_membership=True)


async def test_a_wasm_auth_plugin_is_emitted_with_its_capability(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_upstream(
            s,
            "signed",
            base_url="http://signed.test/v1",
            auth_plugin="hmac_signature",
            auth_params={"ttl_ms": 1000},
            auth_timeout_ms=250,
        )
    snap = (await compile_(factory)).snapshot
    (upstream,) = snap.upstreams
    assert upstream.HasField("auth")
    assert upstream.auth.plugin.plugin == "hmac_signature"
    assert upstream.auth.plugin.params_json == '{"ttl_ms":1000}'
    assert upstream.auth.plugin.timeout_ms == 250
    # A plugin takes no static bearer.
    assert not upstream.auth.secret_ref and not upstream.auth.secret_ciphertext
    assert upstream.api_key_ref == "" and upstream.api_key_ciphertext == b""
    assert list(snap.required_capabilities) == ["auth:hmac_signature"]


async def test_an_upstream_without_auth_is_byte_identical(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_upstream(s, "plain", base_url="http://plain.test/v1")
    snap = (await compile_(factory)).snapshot
    (upstream,) = snap.upstreams
    assert not upstream.HasField("auth")
    expected = pb.Upstream(
        name="plain",
        kind=pb.UPSTREAM_KIND_VLLM,
        base_url="http://plain.test/v1",
        timeout_ms=0,
        stream_usage_mode=pb.STREAM_USAGE_MODE_INCLUDE_USAGE,
    )
    assert upstream.SerializeToString(deterministic=True) == expected.SerializeToString(
        deterministic=True
    )
    assert not any(cap.startswith("auth:") for cap in snap.required_capabilities)


def _snapshot(**upstream) -> pb.Snapshot:
    auth = upstream.pop("auth")
    return pb.Snapshot(
        upstreams=[
            pb.Upstream(
                name="u",
                kind=pb.UPSTREAM_KIND_VLLM,
                base_url="http://u.test/v1",
                auth=auth,
                **upstream,
            )
        ],
        required_capabilities=[f"auth:{auth.plugin.plugin}"],
    )


def _validate(snap: pb.Snapshot) -> None:
    validate_snapshot(snap, max_bytes=1 << 20, max_keys=10)


def test_a_static_bearer_is_not_an_llm_upstream_auth() -> None:
    snap = _snapshot(auth=pb.UpstreamAuth(plugin=pb.PluginRef(plugin="static_bearer")))
    with pytest.raises(SnapshotInvalid, match="built-in"):
        _validate(snap)


@pytest.mark.parametrize("plugin", ["none", "passthrough"])
def test_every_built_in_auth_is_refused(plugin: str) -> None:
    snap = _snapshot(auth=pb.UpstreamAuth(plugin=pb.PluginRef(plugin=plugin)))
    with pytest.raises(SnapshotInvalid, match="built-in"):
        _validate(snap)


def test_auth_and_an_api_key_are_mutually_exclusive() -> None:
    snap = _snapshot(
        auth=pb.UpstreamAuth(plugin=pb.PluginRef(plugin="hmac_signature")),
        api_key_ref="UPSTREAM_KEY",
    )
    with pytest.raises(SnapshotInvalid, match="both auth and an API key"):
        _validate(snap)


def test_an_llm_auth_plugin_takes_no_secret() -> None:
    snap = _snapshot(
        auth=pb.UpstreamAuth(plugin=pb.PluginRef(plugin="hmac_signature"), secret_ref="AUTH_SECRET")
    )
    with pytest.raises(SnapshotInvalid, match="takes no secret"):
        _validate(snap)


def test_a_valid_plugin_snapshot_passes() -> None:
    params = json.dumps({"ttl_ms": 1000}, separators=(",", ":"))
    _validate(
        _snapshot(
            auth=pb.UpstreamAuth(
                plugin=pb.PluginRef(plugin="hmac_signature", params_json=params, timeout_ms=1000)
            )
        )
    )
