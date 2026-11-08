"""CatalogReport ingestion: the first-writer rule, the L3 provenance checks and the bounds.

Everything here runs against committed rows in a cloned database (the `factory` fixture),
because ingestion publishes config versions and a real race needs two sessions.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.mcp import catalog_ingest, hashing
from lumewarp_control_plane.models import (
    A2aAgent,
    ConfigOutbox,
    ConfigVersion,
    ConfigVersionKind,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpServer,
)
from tests import factories
from tests.compiler.helpers import SCOPE, seed_scope


def observe(name: str, description: str = "Echoes text") -> hashing.Observed:
    observed = hashing.observe(
        McpDefinitionKind.TOOL,
        {"name": name, "description": description, "inputSchema": {"type": "object"}},
    )
    assert observed is not None
    return observed


def definition(
    server: str,
    observed: hashing.Observed,
    *,
    auto_approved: bool = True,
    server_id: str = "",
    config_version: int = 0,
    kind: pb.McpDefinitionKind = pb.MCP_DEFINITION_KIND_TOOL,
    hash_override: bytes = b"",
    json_override: str | None = None,
) -> cpb.ObservedDefinition:
    return cpb.ObservedDefinition(
        server=server,
        kind=kind,
        name=observed.name,
        hash=hash_override or observed.hash,
        definition_json=json_override if json_override is not None else observed.canonical_json,
        auto_approved=auto_approved,
        observed_at_unix_ms=1_700_000_000_000,
        server_id=server_id,
        config_version=config_version,
    )


async def ingest(report: cpb.CatalogReport, *, factory, gateway_id: str = "gw-a"):
    async with factory() as s, s.begin():
        return await catalog_ingest.ingest_report(s, gateway_id, report)


async def rows(factory, server: str = "s1") -> list[McpDefinition]:
    async with factory() as s:
        return list(
            (
                await s.scalars(
                    select(McpDefinition)
                    .join(McpServer, McpServer.id == McpDefinition.server_id)
                    .where(McpServer.name == server)
                    .order_by(McpDefinition.kind, McpDefinition.name, McpDefinition.hash)
                )
            ).all()
        )


async def outbox_rows(factory) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(ConfigOutbox)) or 0


async def add_server(factory, name: str = "s1", **kw) -> McpServer:
    async with factory() as s, s.begin():
        return await factories.make_mcp_server(s, name, url=f"http://{name}.test/mcp", **kw)


async def test_first_sight_is_approved_and_a_changed_hash_is_pending(factory) -> None:
    await seed_scope(factory)
    await add_server(factory)
    first = observe("echo")

    result = await ingest(cpb.CatalogReport(definitions=[definition("s1", first)]), factory=factory)
    assert result.approved == 1 and not result.discarded
    stored = await rows(factory)
    assert [(r.name, r.state) for r in stored] == [("echo", McpDefinitionState.APPROVED)]
    # A changed state publishes: the outbox got one entry.
    assert await outbox_rows(factory) == 1

    # The same hash again is "known", with no new version.
    again = await ingest(cpb.CatalogReport(definitions=[definition("s1", first)]), factory=factory)
    assert again.known == 1 and not again.changed
    assert await outbox_rows(factory) == 1

    # A changed definition of a known name is pending, even when the gateway served it.
    changed = observe("echo", "Echoes text, now with feeling")
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("s1", changed)]), factory=factory
    )
    assert result.pending == 1
    states = {(r.name, r.state) for r in await rows(factory)}
    assert states == {
        ("echo", McpDefinitionState.APPROVED),
        ("echo", McpDefinitionState.PENDING),
    }


async def test_two_gateways_reporting_at_once_make_one_decision(factory) -> None:
    """The scope row lock serializes ingestion: of two concurrent TOFU first sights with
    different hashes, exactly one becomes approved and the other is pending."""
    await seed_scope(factory)
    await add_server(factory)
    a, b = observe("echo"), observe("echo", "Echoes text v2")
    report_a = cpb.CatalogReport(definitions=[definition("s1", a, auto_approved=True)])
    report_b = cpb.CatalogReport(definitions=[definition("s1", b, auto_approved=True)])

    results = await asyncio.gather(
        ingest(report_a, factory=factory, gateway_id="gw-a"),
        ingest(report_b, factory=factory, gateway_id="gw-b"),
    )
    assert sum(result.approved for result in results) == 1
    assert sum(result.pending for result in results) == 1
    stored = await rows(factory)
    assert len(stored) == 2
    assert sorted(r.state for r in stored) == [
        McpDefinitionState.APPROVED,
        McpDefinitionState.PENDING,
    ]


async def test_a_name_the_control_plane_knows_is_never_first_sight(factory) -> None:
    """First writer wins: a withheld report makes the name known, so a later report that
    claims it was auto-approved does not approve a different hash."""
    await seed_scope(factory)
    await add_server(factory)
    first, second = observe("echo"), observe("echo", "v2")
    await ingest(
        cpb.CatalogReport(definitions=[definition("s1", first, auto_approved=False)]),
        factory=factory,
    )
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("s1", second, auto_approved=True)]),
        factory=factory,
    )
    assert result.pending == 1
    assert {r.state for r in await rows(factory)} == {McpDefinitionState.PENDING}


async def test_a_rejected_hash_stays_rejected(factory) -> None:
    await seed_scope(factory)
    await add_server(factory)
    first = observe("echo")
    await ingest(
        cpb.CatalogReport(definitions=[definition("s1", first, auto_approved=False)]),
        factory=factory,
    )
    async with factory() as s, s.begin():
        row = await s.scalar(select(McpDefinition))
        row.state = McpDefinitionState.REJECTED
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("s1", first, auto_approved=True)]),
        factory=factory,
    )
    assert result.known == 1
    stored = await rows(factory)
    assert [r.state for r in stored] == [McpDefinitionState.REJECTED]
    # ... and it was only re-sighted, never rewritten.
    assert stored[0].hash == first.hash


async def test_a_stale_server_id_is_discarded(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    other = await add_server(factory, "s2")
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("s1", observe("echo"), server_id=str(other.id))]),
        factory=factory,
    )
    assert result.discarded["stale_id"] == 1
    assert await rows(factory) == []
    assert result.changed is False
    # An empty id is accepted (an older gateway that does not report one).
    result = await ingest(
        cpb.CatalogReport(
            definitions=[definition("s1", observe("echo"), server_id=str(server.id))]
        ),
        factory=factory,
    )
    assert result.approved == 1


async def test_a_hash_that_does_not_describe_its_json_is_discarded(factory) -> None:
    await seed_scope(factory)
    await add_server(factory)
    observed = observe("echo")
    # A different hash for the same JSON, and a name that disagrees with the JSON.
    result = await ingest(
        cpb.CatalogReport(
            definitions=[definition("s1", observed, hash_override=observed.hash[::-1])]
        ),
        factory=factory,
    )
    assert result.discarded["hash_mismatch"] == 1
    wrong_name = definition("s1", observed)
    wrong_name.name = "not-echo"
    result = await ingest(cpb.CatalogReport(definitions=[wrong_name]), factory=factory)
    assert result.discarded["invalid"] == 1
    bad_json = definition("s1", observed, json_override="{not json")
    result = await ingest(cpb.CatalogReport(definitions=[bad_json]), factory=factory)
    assert result.discarded["invalid"] == 1
    assert await rows(factory) == []


async def test_a_report_older_than_the_server_edit_is_discarded(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        s.add(
            ConfigVersion(
                scope=SCOPE,
                version=1,
                payload=b"snapshot",
                digest=b"0" * 32,
                size_bytes=8,
                key_count=0,
                source_revision=0,
                kind=ConfigVersionKind.COMPILE,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    await add_server(factory)  # edited after the version was published
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("s1", observe("echo"), config_version=1)]),
        factory=factory,
    )
    assert result.discarded["stale_version"] == 1
    assert await rows(factory) == []


async def test_an_unknown_server_is_discarded(factory) -> None:
    await seed_scope(factory)
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("ghost", observe("echo"))]), factory=factory
    )
    assert result.discarded["unknown_target"] == 1


async def test_the_pending_and_total_bounds_are_enforced(factory) -> None:
    await seed_scope(factory)
    await add_server(factory)
    async with factory() as s, s.begin():
        server = await s.scalar(select(McpServer))
        for index in range(catalog_ingest.MAX_PENDING_PER_SERVER):
            observed = observe(f"t{index}")
            s.add(
                factories.new_definition(
                    McpDefinitionKind.TOOL,
                    observed,
                    state=McpDefinitionState.PENDING,
                    server_id=server.id,
                )
            )
    result = await ingest(
        cpb.CatalogReport(
            definitions=[definition("s1", observe("one-too-many"), auto_approved=False)]
        ),
        factory=factory,
    )
    assert result.discarded["pending_limit"] == 1
    assert result.pending == 0


async def test_an_oversized_definition_is_discarded(factory) -> None:
    await seed_scope(factory)
    await add_server(factory)
    observed = observe("echo")
    huge = observed.canonical_json[:-1] + ', "x": "' + "a" * catalog_ingest.MAX_DEFINITION_BYTES
    item = definition("s1", observed, json_override=huge + '"}')
    result = await ingest(cpb.CatalogReport(definitions=[item]), factory=factory)
    assert result.discarded["invalid"] == 1


async def test_cards_are_stored_without_publishing_a_version(factory) -> None:
    await seed_scope(factory)
    async with factory() as s, s.begin():
        agent = await factories.make_agent(s, "a1")
        agent_id = str(agent.id)
    card = cpb.AgentCardReport(
        agent="a1",
        card_json='{"name": "agent", "url": "http://up/a2a"}',
        error="",
        fetched_at_unix_ms=1_700_000_000_000,
        agent_id=agent_id,
    )
    result = await ingest(cpb.CatalogReport(cards=[card]), factory=factory)
    assert result.cards == 1
    assert await outbox_rows(factory) == 0
    async with factory() as s:
        agent = await s.scalar(select(A2aAgent))
        assert agent.last_card_json == card.card_json
        assert agent.last_card_error == ""
        assert agent.last_card_at is not None
    # The same card again is not rewritten.
    result = await ingest(cpb.CatalogReport(cards=[card]), factory=factory)
    assert result.cards == 0
    # A fetch error is kept, with the last card still there.
    failed = cpb.AgentCardReport(
        agent="a1", card_json="", error="timed out", fetched_at_unix_ms=1_700_000_001_000
    )
    result = await ingest(cpb.CatalogReport(cards=[failed]), factory=factory)
    assert result.cards == 1
    async with factory() as s:
        agent = await s.scalar(select(A2aAgent))
        assert agent.last_card_error == "timed out"
        assert agent.last_card_json == card.card_json
    # A card whose agent id no longer matches the name is dropped.
    other = cpb.AgentCardReport(agent="a1", card_json='{"x": 1}', agent_id=str(uuid.uuid4()))
    result = await ingest(cpb.CatalogReport(cards=[other]), factory=factory)
    assert result.discarded["stale_id"] == 1


@pytest.mark.parametrize("kind", [pb.MCP_DEFINITION_KIND_UNSPECIFIED, 99])
async def test_an_unusable_definition_kind_is_discarded(factory, kind) -> None:
    await seed_scope(factory)
    await add_server(factory)
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("s1", observe("echo"), kind=kind)]),
        factory=factory,
    )
    assert result.discarded["invalid"] == 1


async def test_prompts_are_hashed_with_their_own_keys(factory) -> None:
    await seed_scope(factory)
    await add_server(factory)
    observed = hashing.observe(McpDefinitionKind.PROMPT, {"name": "greet", "description": "Greets"})
    assert observed is not None
    item = cpb.ObservedDefinition(
        server="s1",
        kind=pb.MCP_DEFINITION_KIND_PROMPT,
        name="greet",
        hash=observed.hash,
        definition_json=observed.canonical_json,
        auto_approved=True,
    )
    await ingest(cpb.CatalogReport(definitions=[item]), factory=factory)
    stored = await rows(factory)
    assert [(r.kind, r.name) for r in stored] == [(McpDefinitionKind.PROMPT, "greet")]


async def test_an_over_long_card_error_drops_the_card_and_keeps_the_last_one(factory) -> None:
    """Review-w1 M4: a 501-byte error is over the 500-byte contract; the card is dropped and
    counted, and the last good card stays (a failed fetch never overwrites it)."""
    await seed_scope(factory)
    async with factory() as s, s.begin():
        await factories.make_agent(s, "a1")
    good = cpb.AgentCardReport(agent="a1", card_json='{"name": "agent"}')
    assert (await ingest(cpb.CatalogReport(cards=[good]), factory=factory)).cards == 1
    bad = cpb.AgentCardReport(agent="a1", card_json="{}", error="e" * 501)
    result = await ingest(cpb.CatalogReport(cards=[bad]), factory=factory)
    assert result.cards == 0 and result.discarded["invalid"] == 1
    async with factory() as s:
        agent = await s.scalar(select(A2aAgent))
        assert agent.last_card_json == '{"name": "agent"}'
        assert agent.last_card_error == ""
    # Exactly 500 bytes is accepted.
    ok = cpb.AgentCardReport(agent="a1", error="e" * 500)
    result = await ingest(cpb.CatalogReport(cards=[ok]), factory=factory)
    assert result.cards == 1
    async with factory() as s:
        agent = await s.scalar(select(A2aAgent))
        assert agent.last_card_error == "e" * 500
        assert agent.last_card_json == '{"name": "agent"}'


async def test_a_report_over_the_item_cap_is_bounded_not_fatal(factory) -> None:
    """Review-w1 M4: per-report bounds drop the excess and count it; nothing raises."""
    await seed_scope(factory)
    await add_server(factory)
    definitions = [definition("s1", observe(f"t{index}")) for index in range(1001)]
    result = await ingest(cpb.CatalogReport(definitions=definitions), factory=factory)
    assert result.stored == catalog_ingest.MAX_DEFINITIONS_PER_REPORT
    assert result.discarded["report_limit"] == 1
    stored = await rows(factory)
    assert len(stored) == catalog_ingest.MAX_DEFINITIONS_PER_REPORT
