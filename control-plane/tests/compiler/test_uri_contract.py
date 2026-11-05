"""API and compiler share a conservative subset of Rust http::Uri."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from lumewarp_control_plane.api.schemas import HealthCheckIn, UpstreamCreate, UpstreamUpdate
from lumewarp_control_plane.compiler import CompileError, compile_scope
from lumewarp_control_plane.compiler.validate import (
    SnapshotInvalid,
    capabilities_used,
    metrics_url_problem,
    upstream_url_problem,
    validate_snapshot,
)
from lumewarp_control_plane.models import Upstream
from tests.compiler.helpers import OCT, SCOPE, add_router, seed_scope
from tests.compiler.test_routing_resilience import _base

INVALID_URLS = [
    "http://exam%2fple.com/metrics",
    "http://example.com:abc/metrics",
    "http://example.com:65536/metrics",
    "http://example.com:/metrics",
    "http://example.com:80:90/metrics",
    "http://user:pw@example.com/metrics",
    "http://@example.com/metrics",
    "http://bad host/metrics",
    "http://example.com/m etrics",
    "http://example.com/m\netrics",
    "http://example.com/m<etrics",
    'http://example.com/m"etrics',
    "http://example.com/m\\etrics",
    "http://example.com/metrics#",
    "http://[::1]extra/metrics",
    "http://[fe80::1%25eth0]/metrics",
    "http://\u00e9xample.com/metrics",
    "http://example.com:\uff11\uff12/metrics",
]
VALID_URLS = [
    "http://localhost/metrics",
    "https://example.com:65535/v1",
    "http://127.0.0.1:0/v1",
    "http://[::1]:8000/v1",
    "https://[2001:db8::1]/v1",
    "http://mock_upstream/v1",
    "http://example.com/a%20b;v=1",
]


@pytest.mark.parametrize("url", INVALID_URLS)
@pytest.mark.parametrize("field", ["base_url", "metrics_url"])
async def test_invalid_uri_rejected_by_api_and_compiler(factory, url, field) -> None:
    for schema in (UpstreamCreate, UpstreamUpdate):
        with pytest.raises(ValidationError):
            schema.model_validate(
                {"name": "u", "kind": "vllm", "base_url": "http://u/v1", field: url}
                if schema is UpstreamCreate
                else {field: url}
            )
    assert (upstream_url_problem if field == "base_url" else metrics_url_problem)(url)
    snap = _base()
    setattr(snap.upstreams[0], field, url)
    snap.required_capabilities.extend(capabilities_used(snap))
    with pytest.raises(SnapshotInvalid):
        validate_snapshot(snap, max_bytes=1 << 26, max_keys=1000)
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_router(session, "name", [("a", None)])
        up = await session.scalar(select(Upstream))
        setattr(up, field, url)
    async with factory() as session:
        compiled = await compile_scope(session, SCOPE, now=OCT, require_local_membership=True)
    assert compiled.warnings
    if field == "base_url":
        assert not compiled.snapshot.upstreams
        assert not compiled.snapshot.routers
    else:
        assert compiled.snapshot.upstreams[0].metrics_url == ""
        assert len(compiled.snapshot.routers) == 1
        assert len(compiled.snapshot.models) == 1


@pytest.mark.parametrize("url", VALID_URLS)
@pytest.mark.parametrize("field", ["base_url", "metrics_url"])
def test_valid_uri_contract(url, field) -> None:
    for schema in (UpstreamCreate, UpstreamUpdate):
        value = schema.model_validate(
            {"name": "u", "kind": "vllm", "base_url": "http://u/v1", field: url}
            if schema is UpstreamCreate
            else {field: url}
        )
        assert getattr(value, field) == url
    snap = _base()
    setattr(snap.upstreams[0], field, url)
    snap.required_capabilities.extend(capabilities_used(snap))
    validate_snapshot(snap, max_bytes=1 << 26, max_keys=1000)


def test_metrics_query_rejected_like_base_query() -> None:
    url = "http://example.com/metrics?name=a%2Fb&v=1"
    assert metrics_url_problem(url) == "must not contain a query"
    assert upstream_url_problem(url)


@pytest.mark.parametrize(
    "path", ["/bad path", "/bad\npath", '/bad"path', "/bad<path", "/bad\\path"]
)
async def test_invalid_health_uri_rejected_by_api_and_compiler(factory, path) -> None:
    with pytest.raises(ValidationError):
        HealthCheckIn(path=path, interval_ms=1000, timeout_ms=100)
    await seed_scope(factory)
    async with factory() as session, session.begin():
        await add_router(session, "name", [("a", None)])
        up = await session.scalar(select(Upstream))
        up.health_check_path = path
        up.health_check_interval_ms = 1000
        up.health_check_timeout_ms = 100
    async with factory() as session:
        with pytest.raises(CompileError, match="health path"):
            await compile_scope(session, SCOPE, now=OCT, require_local_membership=True)


def test_derived_uri_length_is_bounded() -> None:
    assert upstream_url_problem("http://u/" + "a" * 65520)
    assert metrics_url_problem("http://u/" + "a" * 65534)
