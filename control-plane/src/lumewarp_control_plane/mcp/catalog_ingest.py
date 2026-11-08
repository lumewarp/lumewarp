"""CatalogReport ingestion: what a gateway observed at an MCP server or A2A agent.

The control plane cannot fetch these itself for a WASM auth plugin or a user's token, so a
gateway reports the definitions it observed (`ObservedDefinition`) and the agent cards it
fetched (`AgentCardReport`) on the config stream, next to `HealthReport`. Each report adds
to what was reported before; unlike a health report it never replaces it.

The report is untrusted. Nothing here trusts it (L3): the target id must be present and
still match the name, the config version must be one this scope published and must have
been compiled after the target's last write (see `McpServer.config_revision`; a card write
never moves it, so a fetch cannot make the report it came in stale), the hash is
recomputed from `definition_json` and a mismatch discards the entry, and the plan's
bounds (P5) are enforced again. A malformed item (NUL where Postgres text cannot hold it,
a timestamp outside the calendar, JSON an incomplete number or string makes unhashable) is
counted `invalid` and dropped: it never cancels the valid items of the same report or the
gateway's stream.

The decisions:

- First writer wins per (server, kind, name): a name the control plane has never seen is
  approved when the gateway already served it (`auto_approved`, TOFU first sight) *and*
  the server's current review mode allows it; MANUAL never auto-approves, whatever the
  report says. A name the control plane has seen is never served from a new hash without
  a review.
- A hash that was rejected stays rejected: the row for that (server, kind, name, hash) is
  never rewritten, only its `last_seen_at` moves.
- Ingestion takes the scope row lock, then the target rows, in the same order as every API
  write, so two gateways reporting the same name at once still produce one row and one
  decision. A new config version is published only when a set of states actually changed;
  the config outbox coalesces such changes into one compile.
- Cards are display data: the newest card or fetch error replaces the previous one, the
  same card is not rewritten, and no version is published for them.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import outbox
from lumewarp_control_plane._proto.lumewarp.config.v1 import config_service_pb2 as cpb
from lumewarp_control_plane._proto.lumewarp.config.v1 import snapshot_pb2 as pb
from lumewarp_control_plane.mcp import hashing
from lumewarp_control_plane.models import (
    A2aAgent,
    ConfigVersion,
    McpDefinition,
    McpDefinitionKind,
    McpDefinitionState,
    McpReviewMode,
    McpServer,
    Scope,
)
from lumewarp_control_plane.settings import get_settings

# Plan P5, enforced by the gateway and again here.
MAX_PENDING_PER_SERVER = 200
MAX_DEFINITION_BYTES = 64 * 1024
# The gateway's apply-time bound (`targets.rs::MAX_APPROVED_DEFINITIONS`); a snapshot over
# it would fail to compile, so ingest never lets a server get there.
MAX_DEFINITIONS_PER_SERVER = 10_000
MAX_CARD_BYTES = 64 * 1024
# One report, bounded per item and in total. An over-limit item is dropped and counted;
# nothing here aborts a gateway's config stream (a hostile or broken upstream must not cost
# a gateway its other, unrelated reports).
MAX_DEFINITIONS_PER_REPORT = 1000
MAX_CARDS_PER_REPORT = 500
MAX_REPORT_BYTES = 16 * 1024 * 1024
# The card error is sanitized text: the contract with the gateway is 500 bytes.
MAX_CARD_ERROR_BYTES = 500
MAX_NAME_BYTES = hashing.MAX_NAME_BYTES

DEFINITION_KIND = {
    pb.MCP_DEFINITION_KIND_TOOL: McpDefinitionKind.TOOL,
    pb.MCP_DEFINITION_KIND_PROMPT: McpDefinitionKind.PROMPT,
}


@dataclass(frozen=True)
class Observation:
    """One definition a gateway reported (or discovery fetched), already re-checked here."""

    kind: McpDefinitionKind
    name: str
    canonical_json: str
    hash: bytes
    # True when a gateway already serves it (TOFU first sight). Only a name the control
    # plane has never seen can be approved by this.
    auto_approved: bool = False


@dataclass
class IngestResult:
    """What one report or one discovery did; the caller records metrics after the commit."""

    approved: int = 0
    pending: int = 0
    known: int = 0
    cards: int = 0
    changed: bool = False
    discarded: Counter[str] = field(default_factory=Counter)

    @property
    def stored(self) -> int:
        return self.approved + self.pending + self.known

    def counts(self) -> dict[str, int]:
        return {"approved": self.approved, "pending": self.pending, "known": self.known}

    def merge(self, other: IngestResult) -> None:
        self.approved += other.approved
        self.pending += other.pending
        self.known += other.known
        self.cards += other.cards
        self.changed = self.changed or other.changed
        self.discarded.update(other.discarded)


async def lock_scope(session: AsyncSession, scope: str) -> bool:
    """Take the scope row lock (the lock order every writer shares: scope, then target rows).

    Serializing with API edits means a report and an edit cannot deadlock, and two
    gateways reporting at the same moment make one decision between them.
    """
    locked = await session.scalar(select(Scope.name).where(Scope.name == scope).with_for_update())
    return locked is not None


async def _version_revisions(
    session: AsyncSession, scope: str, versions: Iterable[int]
) -> dict[int, int]:
    """The source revision each named config version was compiled from.

    Compaction keeps the metadata, so a retained older version can still be checked. A
    version 0 or one this scope never published has no entry: it cannot be proven and the
    item that claims it is discarded.
    """
    wanted = sorted({version for version in versions if version > 0})
    if not wanted:
        return {}
    rows = await session.execute(
        select(ConfigVersion.version, ConfigVersion.source_revision).where(
            ConfigVersion.scope == scope, ConfigVersion.version.in_(wanted)
        )
    )
    return {version: revision for version, revision in rows}


# How far a gateway's clock may run ahead of the control plane before its card is refused
# (the timestamp is display data, but it must not be arbitrary).
MAX_FETCH_SKEW_MS = 60_000


def _text_ok(*values: str) -> bool:
    """Whether Postgres can store each value as text (it cannot hold U+0000)."""
    return not any("\x00" in value for value in values)


def _card_time(fetched_at_unix_ms: int, now: datetime) -> datetime | None:
    """The reported fetch time, or None when it cannot be a real observation.

    A legal int64 millisecond timestamp is outside `datetime`'s range (M3), and a future
    one cannot describe a fetch that already happened. 0 means "now" (the gateway did not
    say). Never raises.
    """
    if fetched_at_unix_ms <= 0:
        return now
    now_ms = int(now.timestamp() * 1000)
    if fetched_at_unix_ms > now_ms + MAX_FETCH_SKEW_MS:
        return None
    try:
        return min(datetime.fromtimestamp(fetched_at_unix_ms / 1000, UTC), now)
    except (OverflowError, OSError, ValueError):
        return None


def observe(
    kind: pb.McpDefinitionKind, item: cpb.ObservedDefinition, result: IngestResult
) -> Observation | None:
    """The definition as the control plane can trust it, or None (counting the reason)."""
    definition_kind = DEFINITION_KIND.get(kind)
    if definition_kind is None:
        result.discarded["invalid"] += 1
        return None
    # An untrusted upstream can put U+0000 anywhere; Postgres text cannot hold it, and a
    # failed INSERT would abort the transaction of the whole report (M3).
    if not _text_ok(item.name, item.definition_json):
        result.discarded["invalid"] += 1
        return None
    if not 0 < len(item.definition_json.encode("utf-8")) <= MAX_DEFINITION_BYTES:
        result.discarded["invalid"] += 1
        return None
    if not 0 < len(item.name.encode("utf-8")) <= MAX_NAME_BYTES:
        result.discarded["invalid"] += 1
        return None
    observed = hashing.observe_json(definition_kind, item.definition_json)
    if observed is None or observed.name != item.name:
        result.discarded["invalid"] += 1
        return None
    # L3: recompute the hash; a report whose hash does not describe its JSON is dropped.
    if not item.hash or observed.hash != item.hash:
        result.discarded["hash_mismatch"] += 1
        return None
    return Observation(
        kind=definition_kind,
        name=observed.name,
        canonical_json=observed.canonical_json,
        hash=observed.hash,
        auto_approved=item.auto_approved,
    )


async def _counts(session: AsyncSession, server_id: uuid.UUID) -> tuple[int, int]:
    """(pending, total) definitions of one server, for the P5 bounds."""
    rows = await session.execute(
        select(McpDefinition.state, func.count())
        .where(McpDefinition.server_id == server_id)
        .group_by(McpDefinition.state)
    )
    by_state = {state: count for state, count in rows}
    return by_state.get(McpDefinitionState.PENDING, 0), sum(by_state.values())


async def record_observations(
    session: AsyncSession,
    server: McpServer,
    observations: Sequence[Observation],
    *,
    now: datetime | None = None,
) -> IngestResult:
    """Store observations of one server under the first-writer rule.

    The caller holds the scope row lock and has locked `server`, so the read-decide-insert
    sequence cannot interleave with another writer. `auto_approved` only applies to a name
    nobody has seen, and never under MANUAL: a report's flag cannot overrule the review mode.
    """
    result = IngestResult()
    now = now or datetime.now(UTC)
    pending, total = await _counts(session, server.id)
    for observation in observations:
        known = await session.scalar(
            select(McpDefinition.id)
            .where(
                McpDefinition.server_id == server.id,
                McpDefinition.kind == observation.kind,
                McpDefinition.name == observation.name,
                McpDefinition.hash == observation.hash,
            )
            .limit(1)
        )
        if known is not None:
            # A known hash keeps its state (a rejected definition can never come back as
            # first sight); only its last sighting moves.
            await session.execute(
                update(McpDefinition).where(McpDefinition.id == known).values(last_seen_at=now)
            )
            result.known += 1
            continue
        seen_name = await session.scalar(
            select(McpDefinition.id)
            .where(
                McpDefinition.server_id == server.id,
                McpDefinition.kind == observation.kind,
                McpDefinition.name == observation.name,
            )
            .limit(1)
        )
        approved = (
            observation.auto_approved
            and seen_name is None
            and server.review_mode is not McpReviewMode.MANUAL
        )
        if total >= MAX_DEFINITIONS_PER_SERVER:
            result.discarded["definition_limit"] += 1
            continue
        if not approved and pending >= MAX_PENDING_PER_SERVER:
            result.discarded["pending_limit"] += 1
            continue
        session.add(
            McpDefinition(
                server_id=server.id,
                kind=observation.kind,
                name=observation.name,
                hash=observation.hash,
                definition_json=observation.canonical_json,
                state=(McpDefinitionState.APPROVED if approved else McpDefinitionState.PENDING),
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        total += 1
        if approved:
            result.approved += 1
        else:
            pending += 1
            result.pending += 1
        result.changed = True
    await session.flush()
    return result


async def ingest_report(
    session: AsyncSession,
    gateway_id: str,
    report: cpb.CatalogReport,
    *,
    scope: str | None = None,
    now: datetime | None = None,
) -> IngestResult:
    """Record one `CatalogReport` from `gateway_id` in the caller's transaction.

    Every item names the target id and the config version the gateway served (L3). Both are
    required: a missing or mismatched id is `stale_id`, and a version this scope never
    published, or one compiled before the target's last write, is `stale_version`. The
    alternative (trusting whatever a report says) would let an old upstream's definitions
    be recorded against the current one. Bounded before this point (`grpc._MAX_CATALOG_*`)
    and again here; `gateway_id` is unused for the decision (any gateway's observation
    counts the same) but documents the provenance; it is never stored on a definition.
    """
    del gateway_id  # provenance is the authenticated stream, not a stored label
    scope = scope or get_settings().scope
    now = now or datetime.now(UTC)
    result = IngestResult()
    if not await lock_scope(session, scope):
        return result
    revisions = await _version_revisions(
        session,
        scope,
        (
            *(item.config_version for item in report.definitions),
            *(card.config_version for card in report.cards),
        ),
    )
    names = sorted({item.server for item in report.definitions if _text_ok(item.server)})
    servers: dict[str, McpServer] = {}
    if names:
        rows = await session.scalars(
            select(McpServer)
            .where(McpServer.name.in_(names), McpServer.deleted_at.is_(None))
            .order_by(McpServer.id)
            .with_for_update()
        )
        servers = {server.name: server for server in rows}
    grouped: dict[uuid.UUID, tuple[McpServer, list[Observation]]] = {}
    seen_bytes = 0
    for index, item in enumerate(report.definitions):
        if index >= MAX_DEFINITIONS_PER_REPORT:
            result.discarded["report_limit"] += 1
            continue
        seen_bytes += len(item.definition_json.encode("utf-8")) + len(item.name.encode("utf-8"))
        if seen_bytes > MAX_REPORT_BYTES:
            result.discarded["report_limit"] += 1
            continue
        if not _text_ok(item.server):
            result.discarded["invalid"] += 1
            continue
        server = servers.get(item.server)
        if server is None:
            result.discarded["unknown_target"] += 1
            continue
        # L3: an empty id is not "no opinion"; a report we cannot place is discarded.
        if item.server_id != str(server.id):
            result.discarded["stale_id"] += 1
            continue
        # The version must exist and have been compiled after the server's last write: a
        # target created or edited outside the API carries revision 0 and cannot be proven.
        revision = revisions.get(item.config_version)
        if revision is None or not 0 < server.config_revision <= revision:
            result.discarded["stale_version"] += 1
            continue
        observation = observe(item.kind, item, result)
        if observation is None:
            continue
        entry = grouped.setdefault(server.id, (server, []))
        entry[1].append(observation)
    for server, observations in grouped.values():
        result.merge(await record_observations(session, server, observations, now=now))

    agents = await _lock_agents(session, report)
    seen_bytes = 0
    for index, card in enumerate(report.cards):
        if index >= MAX_CARDS_PER_REPORT:
            result.discarded["report_limit"] += 1
            continue
        seen_bytes += len(card.card_json.encode("utf-8")) + len(card.error.encode("utf-8"))
        if seen_bytes > MAX_REPORT_BYTES:
            result.discarded["report_limit"] += 1
            continue
        if not _text_ok(card.agent):
            result.discarded["invalid"] += 1
            continue
        agent = agents.get(card.agent)
        if agent is None:
            result.discarded["unknown_target"] += 1
            continue
        if card.agent_id != str(agent.id):
            result.discarded["stale_id"] += 1
            continue
        revision = revisions.get(card.config_version)
        if revision is None or not 0 < agent.config_revision <= revision:
            result.discarded["stale_version"] += 1
            continue
        if not _text_ok(card.card_json, card.error):
            result.discarded["invalid"] += 1
            continue
        if len(card.card_json.encode("utf-8")) > MAX_CARD_BYTES:
            result.discarded["invalid"] += 1
            continue
        if len(card.error.encode("utf-8")) > MAX_CARD_ERROR_BYTES:
            # Over the contract's bound: drop this card, keep the stream.
            result.discarded["invalid"] += 1
            continue
        card_time = _card_time(card.fetched_at_unix_ms, now)
        if card_time is None:
            result.discarded["invalid"] += 1
            continue
        error = card.error
        # A failed fetch keeps the last card that arrived and only records the error (P7).
        changed = agent.last_card_error != error
        if card.card_json:
            changed = changed or agent.last_card_json != card.card_json
        if not changed:
            continue
        if card.card_json:
            agent.last_card_json = card.card_json
        agent.last_card_error = error
        agent.last_card_at = card_time
        result.cards += 1
    await session.flush()

    if result.changed:
        await outbox.enqueue_recompile(session, scope, "catalog.report")
    return result


async def _lock_agents(session: AsyncSession, report: cpb.CatalogReport) -> dict[str, A2aAgent]:
    """The live agents the report names, row-locked for the card update."""
    names = sorted({card.agent for card in report.cards if _text_ok(card.agent)})
    if not names:
        return {}
    rows = await session.scalars(
        select(A2aAgent)
        .where(A2aAgent.name.in_(names), A2aAgent.deleted_at.is_(None))
        .order_by(A2aAgent.id)
        .with_for_update()
    )
    return {agent.name: agent for agent in rows}
