"""CatalogReport ingestion: the first-writer rule, the L3 provenance checks and the bounds.

Everything here runs against committed rows in a cloned database (the `factory` fixture),
because ingestion publishes config versions and a real race needs two sessions.

Provenance is checked against the `config_revision` an API write leaves on a target and the
`source_revision` a published version was compiled from, so a test that sends a report must
create its target the way the API does (bump the revision, then publish) rather than insert
a bare row.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.compiler import publish_pending
from lumewarp_control_plane.mcp import catalog_ingest, hashing
from lumewarp_control_plane.models import (
    A2aAgent,
    ConfigOutbox,
    ConfigVersion,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
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
    server: McpServer | str,
    observed: hashing.Observed,
    *,
    auto_approved: bool = True,
    server_id: str | None = None,
    config_version: int = 1,
    kind: pb.McpDefinitionKind = pb.MCP_DEFINITION_KIND_TOOL,
    hash_override: bytes = b"",
    json_override: str | None = None,
) -> cpb.ObservedDefinition:
    """One reported definition; a server row supplies the name and its id."""
    bare = isinstance(server, str)
    return cpb.ObservedDefinition(
        server=server if bare else server.name,
        kind=kind,
        name=observed.name,
        hash=hash_override or observed.hash,
        definition_json=json_override if json_override is not None else observed.canonical_json,
        auto_approved=auto_approved,
        observed_at_unix_ms=1_700_000_000_000,
        server_id=server_id if server_id is not None else ("" if bare else str(server.id)),
        config_version=config_version,
    )


def card(
    agent: A2aAgent | str,
    *,
    card_json: str = '{"name": "agent", "url": "http://up/a2a"}',
    error: str = "",
    fetched_at_unix_ms: int = 1_700_000_000_000,
    agent_id: str | None = None,
    config_version: int = 1,
) -> cpb.AgentCardReport:
    bare = isinstance(agent, str)
    return cpb.AgentCardReport(
        agent=agent if bare else agent.name,
        card_json=card_json,
        error=error,
        fetched_at_unix_ms=fetched_at_unix_ms,
        agent_id=agent_id if agent_id is not None else ("" if bare else str(agent.id)),
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


async def publish(factory) -> int:
    """The API's write path: publish what the queued recompiles compiled."""
    version = await publish_pending(factory, SCOPE, require_local_membership=True)
    assert version is not None, "a server write should publish a version"
    return version


async def add_server(factory, name: str = "s1", *, publish_now: bool = True, **kw) -> McpServer:
    """A live server with a real config revision (as the API leaves it)."""
    async with factory() as s, s.begin():
        server = await factories.make_mcp_server(s, name, url=f"http://{name}.test/mcp", **kw)
        server.config_revision = await outbox.enqueue_recompile(s, SCOPE, "test.mcp_server")
    if publish_now:
        await publish(factory)
    return server


async def add_agent(factory, name: str = "a1", *, publish_now: bool = True, **kw) -> A2aAgent:
    async with factory() as s, s.begin():
        agent = await factories.make_agent(s, name, **kw)
        agent.config_revision = await outbox.enqueue_recompile(s, SCOPE, "test.a2a_agent")
    if publish_now:
        await publish(factory)
    return agent


async def test_first_sight_is_approved_and_a_changed_hash_is_pending(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    first = observe("echo")
    before = await outbox_rows(factory)

    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, first)]), factory=factory
    )
    assert result.approved == 1 and not result.discarded
    stored = await rows(factory)
    assert [(r.name, r.state) for r in stored] == [("echo", McpDefinitionState.APPROVED)]
    # A changed state publishes: the outbox got one entry.
    assert await outbox_rows(factory) == before + 1

    # The same hash again is "known", with no new version.
    again = await ingest(
        cpb.CatalogReport(definitions=[definition(server, first)]), factory=factory
    )
    assert again.known == 1 and not again.changed
    assert await outbox_rows(factory) == before + 1

    # A changed definition of a known name is pending, even when the gateway served it.
    changed = observe("echo", "Echoes text, now with feeling")
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, changed)]), factory=factory
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
    server = await add_server(factory)
    a, b = observe("echo"), observe("echo", "Echoes text v2")
    report_a = cpb.CatalogReport(definitions=[definition(server, a, auto_approved=True)])
    report_b = cpb.CatalogReport(definitions=[definition(server, b, auto_approved=True)])

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
    server = await add_server(factory)
    first, second = observe("echo"), observe("echo", "v2")
    await ingest(
        cpb.CatalogReport(definitions=[definition(server, first, auto_approved=False)]),
        factory=factory,
    )
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, second, auto_approved=True)]),
        factory=factory,
    )
    assert result.pending == 1
    assert {r.state for r in await rows(factory)} == {McpDefinitionState.PENDING}


async def test_manual_never_auto_approves_a_report(factory) -> None:
    """M1: a report's `auto_approved` cannot overrule the server's current review mode."""
    await seed_scope(factory)
    manual = await add_server(factory, review_mode=McpReviewMode.MANUAL, publish_now=False)
    tofu = await add_server(factory, "s2", publish_now=False)
    # One version carries both rows (source revision 2; revisions 1 and 2 are both proven).
    version = await publish(factory)
    assert version == 1

    result = await ingest(
        cpb.CatalogReport(definitions=[definition(manual, observe("echo"))]),
        factory=factory,
    )
    assert result.approved == 0 and result.pending == 1
    # TOFU still approves first sight, so the difference is the review mode, not the claim.
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(tofu, observe("echo"))]),
        factory=factory,
    )
    assert result.approved == 1 and result.pending == 0


async def test_a_rejected_hash_stays_rejected(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    first = observe("echo")
    await ingest(
        cpb.CatalogReport(definitions=[definition(server, first, auto_approved=False)]),
        factory=factory,
    )
    async with factory() as s, s.begin():
        row = await s.scalar(select(McpDefinition))
        row.state = McpDefinitionState.REJECTED
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, first, auto_approved=True)]),
        factory=factory,
    )
    assert result.known == 1
    stored = await rows(factory)
    assert [r.state for r in stored] == [McpDefinitionState.REJECTED]
    # ... and it was only re-sighted, never rewritten.
    assert stored[0].hash == first.hash


async def test_a_stale_server_id_is_discarded(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory, publish_now=False)
    other = await add_server(factory, "s2", publish_now=False)
    await publish(factory)
    result = await ingest(
        cpb.CatalogReport(
            definitions=[definition(server, observe("echo"), server_id=str(other.id))]
        ),
        factory=factory,
    )
    assert result.discarded["stale_id"] == 1
    assert await rows(factory) == []
    assert result.changed is False
    # M1: an empty id is not "no opinion" — the report cannot be placed and is discarded.
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, observe("echo"), server_id="")]),
        factory=factory,
    )
    assert result.discarded["stale_id"] == 1
    assert await rows(factory) == []
    # The right id still records.
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, observe("echo"))]), factory=factory
    )
    assert result.approved == 1


async def test_a_version_that_cannot_be_proven_is_discarded(factory) -> None:
    """M1: version 0 and a version this scope never published prove nothing about the target."""
    await seed_scope(factory)
    server = await add_server(factory)
    for version in (0, 999_999):
        result = await ingest(
            cpb.CatalogReport(
                definitions=[definition(server, observe(f"t{version}"), config_version=version)]
            ),
            factory=factory,
        )
        assert result.discarded["stale_version"] == 1
        assert result.stored == 0
    assert await rows(factory) == []


async def test_a_hash_that_does_not_describe_its_json_is_discarded(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    observed = observe("echo")
    # A different hash for the same JSON, and a name that disagrees with the JSON.
    result = await ingest(
        cpb.CatalogReport(
            definitions=[definition(server, observed, hash_override=observed.hash[::-1])]
        ),
        factory=factory,
    )
    assert result.discarded["hash_mismatch"] == 1
    wrong_name = definition(server, observed)
    wrong_name.name = "not-echo"
    result = await ingest(cpb.CatalogReport(definitions=[wrong_name]), factory=factory)
    assert result.discarded["invalid"] == 1
    bad_json = definition(server, observed, json_override="{not json")
    result = await ingest(cpb.CatalogReport(definitions=[bad_json]), factory=factory)
    assert result.discarded["invalid"] == 1
    assert await rows(factory) == []


async def test_a_report_older_than_the_server_edit_is_discarded(factory) -> None:
    """L3: a version compiled before the target's last write may describe another upstream."""
    await seed_scope(factory)
    server = await add_server(factory)
    async with factory() as s, s.begin():
        row = await s.get(McpServer, server.id)
        row.url = "http://s1.test/mcp-changed"
        row.config_revision = await outbox.enqueue_recompile(s, SCOPE, "test.edit")
    # Version 1 no longer covers the edited row, and no later version exists yet.
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, observe("echo"))]), factory=factory
    )
    assert result.discarded["stale_version"] == 1
    assert await rows(factory) == []
    # Once version 2 exists, the same old report is still about the old upstream.
    assert await publish(factory) == 2
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, observe("echo"))]), factory=factory
    )
    assert result.discarded["stale_version"] == 1
    # A report from the new version is trusted.
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, observe("echo"), config_version=2)]),
        factory=factory,
    )
    assert result.approved == 1


async def test_an_unknown_server_is_discarded(factory) -> None:
    await seed_scope(factory)
    result = await ingest(
        cpb.CatalogReport(definitions=[definition("ghost", observe("echo"))]), factory=factory
    )
    assert result.discarded["unknown_target"] == 1


async def test_the_pending_and_total_bounds_are_enforced(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    async with factory() as s, s.begin():
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
            definitions=[definition(server, observe("one-too-many"), auto_approved=False)]
        ),
        factory=factory,
    )
    assert result.discarded["pending_limit"] == 1
    assert result.pending == 0


async def test_an_oversized_definition_is_discarded(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    observed = observe("echo")
    huge = observed.canonical_json[:-1] + ', "x": "' + "a" * catalog_ingest.MAX_DEFINITION_BYTES
    item = definition(server, observed, json_override=huge + '"}')
    result = await ingest(cpb.CatalogReport(definitions=[item]), factory=factory)
    assert result.discarded["invalid"] == 1


async def test_a_nul_where_postgres_text_cannot_hold_it_is_invalid_not_fatal(factory) -> None:
    """M3: a NUL in a reported name would abort the whole report's transaction. It is one
    invalid item; the valid item next to it still lands."""
    await seed_scope(factory)
    server = await add_server(factory)
    nul_name = hashing.observe(
        McpDefinitionKind.TOOL, {"name": "bad\x00name", "description": "nul"}
    )
    assert nul_name is not None and "\x00" in nul_name.name
    report = cpb.CatalogReport(
        definitions=[
            definition(server, nul_name),
            definition(server, observe("fine")),
            # A NUL in the target name must not reach the lookup query either.
            definition("s1\x00junk", observe("also-fine")),
        ]
    )
    result = await ingest(report, factory=factory)
    assert result.discarded["invalid"] == 2
    assert result.approved == 1
    assert [row.name for row in await rows(factory)] == ["fine"]


async def test_a_huge_integer_definition_is_invalid_not_fatal(factory) -> None:
    """M3: `float(10**310)` raises; hashing turns it into an unhashable definition."""
    await seed_scope(factory)
    server = await add_server(factory)
    huge_json = '{"name": "big", "inputSchema": ' + "9" * 310 + "}"
    item = definition(
        server,
        observe("big"),
        json_override=huge_json,
        hash_override=hashing.hash_canonical(huge_json),
    )
    result = await ingest(cpb.CatalogReport(definitions=[item]), factory=factory)
    assert result.discarded["invalid"] == 1
    assert await rows(factory) == []


async def test_cards_are_stored_without_publishing_a_version(factory) -> None:
    await seed_scope(factory)
    agent = await add_agent(factory)
    first = card(agent)
    before = await outbox_rows(factory)
    result = await ingest(cpb.CatalogReport(cards=[first]), factory=factory)
    assert result.cards == 1
    assert await outbox_rows(factory) == before
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_json == first.card_json
        assert stored.last_card_error == ""
        assert stored.last_card_at is not None
    # The same card again is not rewritten.
    result = await ingest(cpb.CatalogReport(cards=[first]), factory=factory)
    assert result.cards == 0
    # A fetch error is kept, with the last card still there.
    failed = card(agent, card_json="", error="timed out", fetched_at_unix_ms=1_700_000_001_000)
    result = await ingest(cpb.CatalogReport(cards=[failed]), factory=factory)
    assert result.cards == 1
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_error == "timed out"
        assert stored.last_card_json == first.card_json
    # A card whose agent id no longer matches the name is dropped.
    other = card(agent, card_json='{"x": 1}', agent_id=str(uuid.uuid4()))
    result = await ingest(cpb.CatalogReport(cards=[other]), factory=factory)
    assert result.discarded["stale_id"] == 1


async def test_card_refreshes_under_one_snapshot_keep_being_recorded(factory) -> None:
    """M2: a card write must not advance the provenance clock.

    Several successful and failed refreshes all report the same applied config version.
    Under the old `updated_at` rule the first card made every later report "stale".
    """
    await seed_scope(factory)
    agent = await add_agent(factory)
    async with factory() as s:
        revision = (await s.scalar(select(A2aAgent))).config_revision
    refreshes = [
        card(agent, card_json='{"name": "first"}'),
        card(agent, card_json="", error="timed out", fetched_at_unix_ms=1_700_000_002_000),
        card(agent, card_json='{"name": "changed"}', fetched_at_unix_ms=1_700_000_003_000),
        card(agent, card_json="", error="HTTP 502", fetched_at_unix_ms=1_700_000_004_000),
    ]
    for refresh in refreshes:
        assert refresh.config_version == 1
        result = await ingest(cpb.CatalogReport(cards=[refresh]), factory=factory)
        assert result.cards == 1, result.discarded
        assert not result.discarded
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_json == '{"name": "changed"}'
        assert stored.last_card_error == "HTTP 502"
        # The row's `updated_at` (the old provenance column) did move past the version, which
        # is exactly what used to make the next report "stale"; the config revision did not.
        version = await s.get(ConfigVersion, (SCOPE, 1))
        assert stored.updated_at > version.created_at
        assert stored.config_revision == revision


async def test_a_hostile_card_timestamp_is_invalid_not_fatal(factory) -> None:
    """M3: a legal int64 millisecond timestamp is outside datetime's range (and in the
    future); the card is one invalid item and the next one in the same report still lands."""
    await seed_scope(factory)
    agent = await add_agent(factory)
    hostile = card(agent, card_json='{"name": "hostile"}', fetched_at_unix_ms=2**63 - 1)
    valid = card(agent, card_json='{"name": "valid"}')
    result = await ingest(cpb.CatalogReport(cards=[hostile, valid]), factory=factory)
    assert result.discarded["invalid"] == 1
    assert result.cards == 1
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_json == '{"name": "valid"}'
        assert stored.last_card_at is not None
        assert stored.last_card_at <= datetime.now(UTC)
    # A slightly fast gateway clock is tolerated, but the stored time never runs ahead.
    future = int(datetime.now(UTC).timestamp() * 1000) + 30_000
    result = await ingest(
        cpb.CatalogReport(
            cards=[card(agent, card_json='{"name": "soon"}', fetched_at_unix_ms=future)]
        ),
        factory=factory,
    )
    assert result.cards == 1
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_at <= datetime.now(UTC)


async def test_a_nul_in_card_text_is_invalid_and_the_next_card_lands(factory) -> None:
    await seed_scope(factory)
    agent = await add_agent(factory)
    result = await ingest(
        cpb.CatalogReport(cards=[card(agent, card_json='{"a": "\x00"}')]), factory=factory
    )
    assert result.discarded["invalid"] == 1
    result = await ingest(cpb.CatalogReport(cards=[card(agent)]), factory=factory)
    assert result.cards == 1


@pytest.mark.parametrize("kind", [pb.MCP_DEFINITION_KIND_UNSPECIFIED, 99])
async def test_an_unusable_definition_kind_is_discarded(factory, kind) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    result = await ingest(
        cpb.CatalogReport(definitions=[definition(server, observe("echo"), kind=kind)]),
        factory=factory,
    )
    assert result.discarded["invalid"] == 1


async def test_prompts_are_hashed_with_their_own_keys(factory) -> None:
    await seed_scope(factory)
    server = await add_server(factory)
    observed = hashing.observe(McpDefinitionKind.PROMPT, {"name": "greet", "description": "Greets"})
    assert observed is not None
    item = cpb.ObservedDefinition(
        server=server.name,
        kind=pb.MCP_DEFINITION_KIND_PROMPT,
        name="greet",
        hash=observed.hash,
        definition_json=observed.canonical_json,
        auto_approved=True,
        server_id=str(server.id),
        config_version=1,
    )
    await ingest(cpb.CatalogReport(definitions=[item]), factory=factory)
    stored = await rows(factory)
    assert [(r.kind, r.name) for r in stored] == [(McpDefinitionKind.PROMPT, "greet")]


async def test_an_over_long_card_error_drops_the_card_and_keeps_the_last_one(factory) -> None:
    """Review-w1 M4: a 501-byte error is over the 500-byte contract; the card is dropped and
    counted, and the last good card stays (a failed fetch never overwrites it)."""
    await seed_scope(factory)
    agent = await add_agent(factory)
    good = card(agent, card_json='{"name": "agent"}')
    assert (await ingest(cpb.CatalogReport(cards=[good]), factory=factory)).cards == 1
    bad = card(agent, card_json="{}", error="e" * 501)
    result = await ingest(cpb.CatalogReport(cards=[bad]), factory=factory)
    assert result.cards == 0 and result.discarded["invalid"] == 1
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_json == '{"name": "agent"}'
        assert stored.last_card_error == ""
    # Exactly 500 bytes is accepted.
    ok = card(agent, card_json="", error="e" * 500)
    result = await ingest(cpb.CatalogReport(cards=[ok]), factory=factory)
    assert result.cards == 1
    async with factory() as s:
        stored = await s.scalar(select(A2aAgent))
        assert stored.last_card_error == "e" * 500
        assert stored.last_card_json == '{"name": "agent"}'


async def test_a_report_over_the_item_cap_is_bounded_not_fatal(factory) -> None:
    """Review-w1 M4: per-report bounds drop the excess and count it; nothing raises."""
    await seed_scope(factory)
    server = await add_server(factory)
    definitions = [definition(server, observe(f"t{index}")) for index in range(1001)]
    result = await ingest(cpb.CatalogReport(definitions=definitions), factory=factory)
    assert result.stored == catalog_ingest.MAX_DEFINITIONS_PER_REPORT
    assert result.discarded["report_limit"] == 1
    stored = await rows(factory)
    assert len(stored) == catalog_ingest.MAX_DEFINITIONS_PER_REPORT


async def test_a_card_report_from_another_version_is_discarded(factory) -> None:
    await seed_scope(factory)
    agent = await add_agent(factory)
    result = await ingest(
        cpb.CatalogReport(cards=[card(agent, config_version=999_999)]), factory=factory
    )
    assert result.discarded["stale_version"] == 1
    assert result.cards == 0
    # An edit after version 1 also invalidates a card observed at version 1.
    async with factory() as s, s.begin():
        row = await s.get(A2aAgent, agent.id)
        row.url = "http://a1.test/a2a-changed"
        row.config_revision = await outbox.enqueue_recompile(s, SCOPE, "test.agent_edit")
    assert await publish(factory) == 2
    result = await ingest(cpb.CatalogReport(cards=[card(agent)]), factory=factory)
    assert result.discarded["stale_version"] == 1
    result = await ingest(cpb.CatalogReport(cards=[card(agent, config_version=2)]), factory=factory)
    assert result.cards == 1
