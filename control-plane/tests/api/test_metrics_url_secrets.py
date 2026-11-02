"""A metrics URL must not carry credentials into the database or the audit log.

`base_url` already refuses userinfo and query strings; `metrics_url` is the other URL copied
into the API response, the snapshot and audit, so it follows the same rule.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from lumewarp_control_plane.api.schemas import UpstreamCreate, UpstreamUpdate
from lumewarp_control_plane.compiler.validate import metrics_url_problem
from lumewarp_control_plane.models import AuditLog, Upstream
from tests.api.helpers import ADMIN_AUTH, suffix

BASE = "http://127.0.0.1:9/v1"
# Credentials in the URL forms the validator must refuse: a query string or userinfo.
BAD_METRICS_URLS = [
    "https://metrics.example.test/metrics?token=private-value",
    "https://metrics.example.test/metrics?name=a%2Fb&v=1",
    "https://user:private-value@metrics.example.test/metrics",
    "https://@metrics.example.test/metrics",
]


def _create(client, **body):
    body = {"name": f"up-{suffix()}", "kind": "vllm", "base_url": BASE, **body}
    return client.post("/api/v1/upstreams", json=body, headers=ADMIN_AUTH)


@pytest.mark.parametrize("url", BAD_METRICS_URLS)
def test_metrics_url_credentials_are_rejected_by_every_schema(url) -> None:
    assert metrics_url_problem(url)
    with pytest.raises(ValidationError):
        UpstreamCreate.model_validate(
            {"name": "u", "kind": "vllm", "base_url": BASE, "metrics_url": url}
        )
    with pytest.raises(ValidationError):
        UpstreamUpdate.model_validate({"metrics_url": url})


@pytest.mark.parametrize("url", BAD_METRICS_URLS)
async def test_api_refusal_stores_nothing_and_audits_nothing(client, committed, url) -> None:
    upstreams_before = await committed.scalar(select(func.count()).select_from(Upstream))
    # Break-glass requests are audited even when rejected, so count mutation rows only.
    mutations_before = await committed.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action.in_(("upstream.create", "upstream.update")))
    )

    response = _create(client, metrics_url=url)

    assert response.status_code == 422, response.text
    assert "metrics_url" in response.text
    assert await committed.scalar(select(func.count()).select_from(Upstream)) == upstreams_before
    assert (
        await committed.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action.in_(("upstream.create", "upstream.update")))
        )
        == mutations_before
    )
    assert await committed.scalar(select(Upstream.id).where(Upstream.metrics_url == url)) is None
    # Defense in depth: no row written by another test or path contains the credential.
    rows = (await committed.scalars(select(AuditLog))).all()
    assert all(url not in str(row.details) for row in rows)


async def test_patch_refusal_keeps_the_stored_value_and_writes_no_audit(client, committed) -> None:
    created = _create(client, metrics_url="https://metrics.example.test/metrics")
    assert created.status_code == 201, created.text
    upstream = created.json()
    bad = BAD_METRICS_URLS[0]

    response = client.patch(
        f"/api/v1/upstreams/{upstream['id']}", json={"metrics_url": bad}, headers=ADMIN_AUTH
    )

    assert response.status_code == 422, response.text
    row = await committed.get(Upstream, uuid.UUID(upstream["id"]))
    assert row is not None and row.metrics_url == "https://metrics.example.test/metrics"
    audits = (
        await committed.scalars(
            select(AuditLog).where(
                AuditLog.action == "upstream.update",
                AuditLog.resource_id == upstream["id"],
            )
        )
    ).all()
    assert audits == []
    everything = (await committed.scalars(select(AuditLog))).all()
    assert all(bad not in str(row.details) for row in everything)


async def test_clean_metrics_url_is_stored_and_audited(client, committed) -> None:
    created = _create(client, metrics_url="https://metrics.example.test:8443/metrics")
    assert created.status_code == 201, created.text
    upstream = created.json()
    clean = "https://metrics.example.test:8443/other"

    patched = client.patch(
        f"/api/v1/upstreams/{upstream['id']}", json={"metrics_url": clean}, headers=ADMIN_AUTH
    )

    assert patched.status_code == 200, patched.text
    row = await committed.get(Upstream, uuid.UUID(upstream["id"]))
    assert row is not None and row.metrics_url == clean
    audit = await committed.scalar(
        select(AuditLog)
        .where(AuditLog.action == "upstream.update", AuditLog.resource_id == upstream["id"])
        .order_by(AuditLog.id.desc())
    )
    assert audit is not None
    assert audit.details["changes"]["metrics_url"] == clean
