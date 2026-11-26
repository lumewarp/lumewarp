"""The Alertmanager webhook receiver and the signed-in status view.

The receiver is authenticated by its own fixed bearer token, never by a session or CSRF:
Alertmanager has no browser session. The status view only shows firing alerts that carry
`lumewarp_status` and touch a name the caller can call, so it never leaks another tenant's
catalog or upstream names.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from lumewarp_control_plane.alerts import IncomingAlert, receive
from lumewarp_control_plane.alerts.status import StatusItem, status_items
from lumewarp_control_plane.api.common import SessionDep, SettingsDep
from lumewarp_control_plane.api.schemas import ReadModel
from lumewarp_control_plane.authz import Action, Authz
from lumewarp_control_plane.errors import ApiError, not_found, unauthorized

router = APIRouter(tags=["usage"])

# 1 MiB is far above a full Alertmanager payload and still bounds a hostile body.
MAX_BODY_BYTES = 1024 * 1024
MAX_ALERTS_PER_PAYLOAD = 1000

alertmanager_scheme = HTTPBearer(
    scheme_name="AlertmanagerToken",
    description=(
        "The fixed `LUMEWARP_ALERTMANAGER_TOKEN` value, sent as "
        "`Authorization: Bearer <token>`. It authenticates only this receiver; sessions, "
        "CSRF tokens and personal access tokens are not accepted here."
    ),
    auto_error=False,
)


async def _read_body(request: Request) -> bytes:
    """The body, read in chunks and abandoned as soon as it passes the cap."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "body exceeds 1 MiB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large", "body exceeds 1 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


class AlertmanagerAlert(BaseModel):
    """One entry of the Alertmanager webhook v4 `alerts` array."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    status: Literal["firing", "resolved"] = Field(
        description="The alert's current state in Alertmanager."
    )
    labels: dict[str, str] = Field(
        description=(
            "Alert labels. `alertname`, `zone`, `requested`, `model`, `upstream`, "
            "`severity`, `lumewarp_status` and `lumewarp_summary` are the ones Lumewarp acts "
            "on; the rest are stored verbatim."
        )
    )
    annotations: dict[str, str] = Field(
        description=(
            "Alert annotations. `lumewarp_summary` is shown to users; `summary` and "
            "`description` are shown to installation admins."
        )
    )
    startsAt: AwareDatetime = Field(description="When the alert started, with a timezone.")
    endsAt: AwareDatetime = Field(
        description=(
            "When the alert ended, with a timezone; Alertmanager's year-1 sentinel means "
            "no end while firing."
        )
    )
    generatorURL: str = Field(
        default="", description="Link back to the Prometheus expression that fired."
    )
    fingerprint: str = Field(
        min_length=1,
        max_length=64,
        description="Alertmanager's stable fingerprint; with `startsAt` it identifies the row.",
    )


class AlertmanagerPayload(BaseModel):
    """The Alertmanager webhook v4 body (`version`, `alerts`, and fields Lumewarp ignores)."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    version: Literal["4"] = Field(description="The webhook version; only `4` is accepted.")
    alerts: list[AlertmanagerAlert] = Field(
        default_factory=list,
        max_length=MAX_ALERTS_PER_PAYLOAD,
        description="The alerts of this delivery; at most 1000.",
    )


class AlertReceivedOut(ReadModel):
    received: int = Field(description="Number of alerts stored from this delivery.")


class StatusItemOut(ReadModel):
    name: str = Field(description="The name the caller can call.")
    zone: str = Field(description="The zone the alert applies to.")
    state: str = Field(description="The alert's `lumewarp_status` label.")
    summary: str | None = Field(
        default=None, description="The `lumewarp_summary` annotation, when present."
    )
    since: datetime = Field(description="When the alert started, with a timezone.")
    incident_id: uuid.UUID | None = Field(
        default=None,
        description="The incident of the alert, for the incident link; installation admins only.",
    )


class StatusOut(ReadModel):
    items: list[StatusItemOut]


def _payload_schema() -> dict:
    """The body's JSON schema with nested definitions inlined, for the OpenAPI document."""
    schema = AlertmanagerPayload.model_json_schema(mode="validation")
    defs = schema.pop("$defs", {})

    def inline(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return inline(defs[ref.rsplit("/", 1)[1]])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(value) for value in node]
        return node

    return inline(schema)  # type: ignore[return-value]


@router.post(
    "/alerts/alertmanager",
    response_model=AlertReceivedOut,
    operation_id="receiveAlertmanagerWebhook",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _payload_schema()}},
        }
    },
)
async def receive_alertmanager(
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    _scheme: Annotated[HTTPAuthorizationCredentials | None, Security(alertmanager_scheme)] = None,
) -> AlertReceivedOut:
    """Store one Alertmanager webhook v4 delivery and return `{received: n}`.

    Authenticated by `Authorization: Bearer <LUMEWARP_ALERTMANAGER_TOKEN>`, compared in
    constant time, before the body is read: an unset token answers 404 and a wrong one 401
    whatever the body is. The body is then read in chunks and refused with 413 past 1 MiB,
    and validated (422): version 4, at most 1000 alerts. There is no request-rate limiter;
    the token and these caps are the bound. Replaying the same delivery is idempotent:
    alerts are upserted on `(fingerprint, startsAt)` and their target list is rewritten from
    the labels (`requested`, `model`, `upstream`, scoped by `zone`). A new alert joins the
    overlapping incident of the same alertname, zone and targets, or opens one; it never
    moves afterwards. This receiver is not
    audited per call.
    """
    configured = settings.alertmanager_token
    if configured is None:
        raise not_found("the alertmanager receiver is not configured")
    scheme, _, provided = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not provided:
        raise unauthorized("missing alertmanager bearer token")
    if not hmac.compare_digest(provided.strip(), configured.get_secret_value()):
        raise unauthorized("invalid alertmanager bearer token")
    raw = await _read_body(request)
    try:
        body = AlertmanagerPayload.model_validate_json(raw)
    except ValidationError as error:
        raise ApiError(
            422, "invalid_payload", "not an Alertmanager v4 payload: " + _first_error(error)
        ) from None
    incoming = [
        IncomingAlert(
            status=alert.status,
            labels=alert.labels,
            annotations=alert.annotations,
            starts_at=alert.startsAt,
            ends_at=alert.endsAt,
            generator_url=alert.generatorURL or None,
            fingerprint=alert.fingerprint,
        )
        for alert in body.alerts
    ]
    async with session.begin():
        received = await receive(session, incoming)
    return AlertReceivedOut(received=received)


def _first_error(error: ValidationError) -> str:
    first = error.errors(include_url=False, include_input=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "body"
    return f"{location}: {first['msg']}"


def _status_out(item: StatusItem) -> StatusItemOut:
    return StatusItemOut(
        name=item.name,
        zone=item.zone,
        state=item.state,
        summary=item.summary,
        since=item.since,
        incident_id=item.incident_id,
    )


@router.get("/status", response_model=StatusOut, operation_id="getStatus")
async def get_status(authz: Authz, session: SessionDep) -> StatusOut:
    """Firing user-visible alerts that touch names the caller can call, one item per name
    and zone.

    Every signed-in caller may read this. A name appears only when the caller's effective
    models in that zone contain it and the zone's snapshot deploys it; an upstream target
    contributes the models deployed on that upstream. Alerts without `lumewarp_status` are
    admin-only and never appear here. Installation admins also get `incident_id` for the
    incident link; no upstream names, labels or other annotations are returned.
    """
    await authz.require(Action.CATALOG_READ)
    return StatusOut(items=[_status_out(item) for item in await status_items(session, authz)])
