"""The whole control plane in-process: REST + gRPC + compiler loop + schedulers, against a
fresh Postgres database, driven by real gRPC clients acting as two gateways.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
import httpx
import pytest
from sqlalchemy import select

from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2_grpc as cpb_grpc
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2, usage_pb2_grpc
from lumewarp_control_plane.app import create_app
from lumewarp_control_plane.models import ConfigVersion, Scope, ScopeType, UsageTotal
from lumewarp_control_plane.settings import Settings
from tests.compiler.helpers import apply_delta
from tests.conftest import migrated_database
from tests.integration.grpc_probe import probe

SCOPE = "default"
ADMIN: dict[str, str] = {}


@dataclass
class ControlPlane:
    app: object
    http: httpx.AsyncClient
    grpc_target: str
    fatal: list[str]

    async def post(self, path: str, body: dict | None = None) -> dict:
        r = await self.http.post(f"/api/v1{path}", json=body or {}, headers=ADMIN)
        assert r.status_code in (200, 201), (path, r.status_code, r.text)
        return r.json()


@pytest.fixture
async def cp() -> AsyncIterator[ControlPlane]:
    async with migrated_database() as url:
        settings = Settings(
            database_url=url,
            grpc_bind="127.0.0.1:0",
            metrics_bind="127.0.0.1:0",
            dev_mode=True,
            break_glass_password="itest-admin",
            session_cookie_secure=False,
            scope=SCOPE,
            background_enabled=True,
        )
        fatal: list[str] = []
        app = create_app(settings, fatal_handler=fatal.append)
        async with app.state.session_factory() as s, s.begin():
            s.add(Scope(name=SCOPE))  # what `bootstrap` does
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://cp") as http:
                login = await http.post(
                    "/api/v1/session/break-glass", json={"password": "itest-admin"}
                )
                assert login.status_code == 204
                info = await http.get("/api/v1/session")
                ADMIN["X-Lumewarp-CSRF"] = info.json()["csrf_token"]
                port = app.state.runtime.grpc_port
                yield ControlPlane(app, http, f"127.0.0.1:{port}", fatal)
        # Graceful shutdown leaves no background tasks behind.
        assert all(t.done() for t in app.state.runtime.tasks.values())
        leftovers = [
            t for t in asyncio.all_tasks() if t.get_name().startswith("lumewarp:") and not t.done()
        ]
        assert leftovers == []
        assert fatal == []


class Gateway:
    """A minimal gateway: applies updates to its snapshot and ACKs them."""

    def __init__(self, target: str, token: str, gateway_id: str) -> None:
        self.channel = grpc.aio.insecure_channel(target)
        self.metadata = (("authorization", f"Bearer {token}"),)
        self.gateway_id = gateway_id
        self.snapshot = pb.Snapshot()
        # ConfigUpdate.target_digest of self.snapshot, echoed in Hello.have_digest.
        self.digest = b""
        self.call = None

    async def connect(self, have: tuple[pb.Snapshot, bytes] | None = None) -> None:
        if have is not None:
            self.snapshot, self.digest = have
        self.call = cpb_grpc.ConfigServiceStub(self.channel).Stream(metadata=self.metadata)
        await self.call.write(
            cpb.GatewayMessage(
                hello=cpb.Hello(
                    gateway_id=self.gateway_id,
                    instance_id="itest-1",
                    have_version=self.snapshot.version,
                    have_digest=self.digest,
                    scope=self.snapshot.scope,
                    gateway_version="itest",
                )
            )
        )

    async def next_update(self, timeout: float = 5.0) -> cpb.ConfigUpdate:
        update = await asyncio.wait_for(self.call.read(), timeout)
        if update.WhichOneof("update") == "full":
            self.snapshot = update.full
        else:
            self.snapshot = apply_delta(self.snapshot, update.delta)
        self.digest = update.target_digest
        await self.call.write(cpb.GatewayMessage(ack=cpb.Ack(version=self.snapshot.version)))
        return update

    async def until(self, predicate, timeout: float = 5.0) -> list[cpb.ConfigUpdate]:
        """Apply updates until predicate(snapshot) holds; return the updates received."""
        updates = []
        deadline = time.monotonic() + timeout
        while not predicate(self.snapshot):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{self.gateway_id}: condition not met in {timeout}s"
            updates.append(await self.next_update(remaining))
        return updates

    def key(self, key_id: str) -> pb.ApiKey | None:
        return next((k for k in self.snapshot.api_keys if k.key_id == key_id), None)

    async def close(self) -> None:
        if self.call is not None:
            self.call.cancel()
        await self.channel.close()


async def _load_version(cp: ControlPlane, version: int) -> tuple[pb.Snapshot, bytes]:
    async with cp.app.state.session_factory() as s:
        row = await s.get(ConfigVersion, (SCOPE, version))
        return pb.Snapshot.FromString(row.payload), row.digest


async def test_end_to_end(cp: ControlPlane) -> None:
    r = await cp.http.get("/readyz")
    assert r.status_code == 200, r.text
    assert set(r.json()["checks"]) >= {"grpc", "compiler", "rollover"}

    # Routing, a team, a service account and two keys via REST.
    up = await cp.post("/upstreams", {"name": "vllm", "kind": "vllm", "base_url": "http://v/v1"})
    dep = await cp.post(
        "/models",
        {
            "name": "llama-a",
            "upstream_id": up["id"],
            "upstream_model": "llama",
            "model_type": "llm",
            "price_input_nano_usd_per_mtok": 1_000,
            "price_output_nano_usd_per_mtok": 2_000,
        },
    )
    await cp.post("/routers", {"name": "llama", "members": [{"model_id": dep["id"]}]})
    org = await cp.post("/orgs", {"name": "ml"})
    team = await cp.post("/teams", {"name": "ml", "org_id": org["id"]})
    account = await cp.post(
        f"/teams/{team['id']}/service-accounts", {"name": "chat", "budget_nano_usd": 1_000}
    )
    k1 = (
        await cp.post(
            f"/teams/{team['id']}/keys", {"name": "k1", "service_account_id": account["id"]}
        )
    )["key"]
    k2 = (
        await cp.post(
            f"/teams/{team['id']}/keys", {"name": "k2", "service_account_id": account["id"]}
        )
    )["key"]
    tok_a = (await cp.post("/gateways", {"gateway_id": "gw-a"}))["secret"]
    tok_b = (await cp.post("/gateways", {"gateway_id": "gw-b"}))["secret"]

    # Gateway A: first update is a full snapshot; it converges on both keys.
    a = Gateway(cp.grpc_target, tok_a, "gw-a")
    b = Gateway(cp.grpc_target, tok_b, "gw-b")
    try:
        await a.connect()
        first = await a.next_update()
        assert first.WhichOneof("update") == "full"
        await a.until(lambda s: a.key(k1["id"]) is not None and a.key(k2["id"]) is not None)
        assert [r.name for r in a.snapshot.routers] == ["llama"]
        assert [m.name for m in a.snapshot.models] == ["llama-a"]
        assert a.key(k1["id"]).status == pb.KEY_STATUS_ACTIVE
        before_revoke = a.snapshot.version

        # Revoke k1 via REST: A gets a delta removing it within 2 s.
        started = time.monotonic()
        await cp.post(f"/keys/{k1['id']}/revoke")
        updates = await a.until(lambda s: a.key(k1["id"]) is None, timeout=2.0)
        assert time.monotonic() - started < 2.0
        assert all(u.WhichOneof("update") == "delta" for u in updates)
        assert k1["id"] in updates[-1].delta.remove_api_keys
        assert updates[0].delta.base_version == before_revoke

        # Gateway B connects later from an older retained version: it gets a delta.
        old = await _load_version(cp, before_revoke)
        await b.connect(have=old)
        update = await b.next_update()
        assert update.WhichOneof("update") == "delta"
        assert update.delta.base_version == before_revoke
        assert b.snapshot == a.snapshot

        # Usage over the owner budget flips k2 to BUDGET_EXCEEDED on both gateways.
        now_ms = int(time.time() * 1000)
        event = usage_pb2.UsageEvent(
            request_id=str(uuid.uuid4()),
            timestamp_unix_ms=now_ms,
            started_at_unix_ms=now_ms - 100,
            key_id=k2["id"],
            team_id=team["id"],
            requested="llama",
            model="llama-a",
            endpoint="chat_completions",
            status=200,
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            cost_nano_usd=2_000,
            cost_known=True,
            duration_ms=100,
            config_version=a.snapshot.version,
            usage_state=usage_pb2.USAGE_STATE_REPORTED,
            outcome=usage_pb2.REQUEST_OUTCOME_COMPLETED,
        )
        usage = usage_pb2_grpc.UsageServiceStub(a.channel)
        request = usage_pb2.ReportRequest(gateway_id="gw-a", events=[event])
        resp = await usage.Report(request, metadata=a.metadata)
        assert resp.accepted == 1
        # Replay is idempotent.
        assert (await usage.Report(request, metadata=a.metadata)).accepted == 1

        exceeded = pb.KEY_STATUS_BUDGET_EXCEEDED
        for gw in (a, b):
            updates = await gw.until(lambda s, gw=gw: gw.key(k2["id"]).status == exceeded)
            assert all(u.WhichOneof("update") == "delta" for u in updates)
        async with cp.app.state.session_factory() as s:
            spent = await s.scalar(
                select(UsageTotal.cost_nano_usd).where(
                    UsageTotal.scope_type == ScopeType.SERVICE_ACCOUNT,
                    UsageTotal.scope_id == uuid.UUID(account["id"]),
                )
            )
        assert spent == 2_000

        # Gateway status is visible over REST.
        r = await cp.http.get("/api/v1/gateways", headers=ADMIN)
        assert {g["gateway_id"] for g in r.json()["items"] if g["connected"]} == {"gw-a", "gw-b"}

        # The standalone probe (used for manual checks) sees the same version.
        probe_token = (await cp.post("/gateways", {"gateway_id": "probe"}))["secret"]
        version, keys = await probe(cp.grpc_target, probe_token, "probe")
        assert (version, keys) == (a.snapshot.version, len(a.snapshot.api_keys))

        # The metrics listener (its own port, not the admin API) reflects all of the above.
        # The probe's stream is torn down asynchronously on the server: wait for it.
        async with asyncio.timeout(5):
            while (samples := await _scrape(cp))[("lumewarp_cp_gateway_sessions", ())] != 2:
                await asyncio.sleep(0.02)
        assert samples[("lumewarp_cp_config_version", ())] == a.snapshot.version
        assert samples[("lumewarp_cp_compile_duration_seconds_count", ())] >= 3
        assert samples[("lumewarp_cp_compile_failures_total", ())] == 0
        assert samples[("lumewarp_cp_gateway_acks_total", (("result", "ack"),))] >= 4
        assert samples[("lumewarp_cp_gateway_acks_total", (("result", "nack"),))] == 0
        assert samples[("lumewarp_cp_usage_events_ingested_total", ())] == 2
        builtin_allow = (("backend", "builtin"), ("decision", "allow"))
        assert ("lumewarp_cp_authz_checks_total", builtin_allow) in samples
        assert "lumewarp_cp_outbox_pending" in {name for name, _ in samples}
    finally:
        await a.close()
        await b.close()


async def _scrape(cp: ControlPlane) -> dict[tuple[str, tuple], float]:
    from prometheus_client.parser import text_string_to_metric_families

    port = cp.app.state.runtime.metrics_server.port
    async with httpx.AsyncClient() as http:
        r = await http.get(f"http://127.0.0.1:{port}/metrics")
    assert r.status_code == 200
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(r.text)
        for s in family.samples
    }


async def test_grpc_crash_fails_readiness_and_requests_exit(cp: ControlPlane) -> None:
    runtime = cp.app.state.runtime
    failed = asyncio.Event()

    def fatal_handler(name):
        cp.fatal.append(name)
        failed.set()

    cp.app.state.fatal_handler = fatal_handler
    await runtime.grpc_server.stop(0)
    async with asyncio.timeout(5):
        await failed.wait()
    r = await cp.http.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["grpc"] != "ok"
    assert cp.fatal == ["grpc"]
    cp.fatal.clear()  # expected here; the fixture asserts no other fatal exits


async def test_unknown_api_path_is_json_404(cp: ControlPlane) -> None:
    r = await cp.http.get("/api/v1/does-not-exist", headers=ADMIN)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_serve_refuses_multiple_workers() -> None:
    from lumewarp_control_plane.__main__ import main

    with pytest.raises(SystemExit, match="single process"):
        main(["serve", "--workers", "2"])
