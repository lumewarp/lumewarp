from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lumewarp_control_plane.db import SessionFactory, create_session_factory
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    NotifierChannel,
    NotifierOutbox,
    Org,
    OutboxStatus,
    ScopeType,
)
from lumewarp_control_plane.notifier import BudgetEvent, deliver_pending


@pytest.fixture(scope="module")
async def c4_engine():
    from tests.conftest import migrated_database

    async with migrated_database() as url:
        engine = create_async_engine(url)
        try:
            yield engine
        finally:
            await engine.dispose()


@pytest.fixture(scope="module")
async def session_factory(c4_engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(c4_engine)


async def _webhook_server():
    state = {"requests": 0, "bodies": []}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            body = await reader.readexactly(content_length) if content_length else b""
            state["requests"] += 1
            state["bodies"].append(body)
            status = b"500 Internal Server Error" if state["requests"] == 1 else b"204 No Content"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}/budget", state


@pytest.mark.asyncio
async def test_webhook_outbox_retries_and_marks_delivered(session_factory: SessionFactory):
    server, url, state = await _webhook_server()
    org_id = uuid7()
    event = BudgetEvent(
        kind="soft",
        scope_type=ScopeType.ORG,
        scope_id=org_id,
        org_id=org_id,
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=80,
        budget_nano_usd=100,
    )
    async with session_factory() as session, session.begin():
        session.add(Org(id=org_id, name=f"notifier-org-{uuid7().hex}"))
        await session.flush()
        channel = NotifierChannel(
            name=f"webhook-{uuid7().hex}", kind="webhook", org_id=org_id, config={"url": url}
        )
        session.add(channel)
        await session.flush()
        outbox_row = NotifierOutbox(
            dedupe_key=f"test:{uuid7()}",
            channel_id=channel.id,
            event_type="budget.soft",
            payload=event.to_payload(),
            status=OutboxStatus.PENDING,
            next_attempt_at=datetime.now(UTC),
        )
        session.add(outbox_row)
        await session.flush()
        row_id = outbox_row.id

    try:
        assert await deliver_pending(session_factory) == 0
        async with session_factory() as session:
            row = await session.get(NotifierOutbox, row_id)
            assert row is not None
            assert row.status is OutboxStatus.PENDING
            assert row.attempts == 1
            assert row.last_error == "HTTPStatusError"
            assert row.next_attempt_at > datetime.now(UTC)

        async with session_factory() as session, session.begin():
            row = await session.get(NotifierOutbox, row_id)
            assert row is not None
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

        assert await deliver_pending(session_factory) == 1
        async with session_factory() as session:
            row = await session.get(NotifierOutbox, row_id)
            assert row is not None
            assert row.status is OutboxStatus.DONE
            assert row.delivered_at is not None
            assert row.attempts == 1
        assert state["requests"] == 2
        assert b'"scope_type":"org"' in state["bodies"][1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_notifier_plugin_is_invoked(session_factory: SessionFactory, monkeypatch):
    from lumewarp_control_plane import plugins

    received = []

    class TestPlugin:
        api_version = plugins.PLUGIN_API_VERSION

        async def send(self, event: BudgetEvent, config):
            received.append((event, config))

    class Loaded:
        name = "test-notifier"
        plugin = TestPlugin()

    monkeypatch.setattr(
        plugins, "load", lambda group: [Loaded()] if group == plugins.NOTIFIER else []
    )
    org_id = uuid7()
    event = BudgetEvent(
        kind="hard",
        scope_type=ScopeType.KEY,
        scope_id=uuid7(),
        org_id=org_id,
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=100,
        budget_nano_usd=100,
    )
    async with session_factory() as session, session.begin():
        session.add(Org(id=org_id, name=f"plugin-org-{uuid7().hex}"))
        await session.flush()
        channel = NotifierChannel(
            name=f"plugin-{uuid7().hex}",
            kind="test-notifier",
            org_id=org_id,
            config={"target": "test"},
        )
        session.add(channel)
        await session.flush()
        row = NotifierOutbox(
            dedupe_key=f"plugin:{uuid7()}",
            channel_id=channel.id,
            event_type="budget.hard",
            payload=event.to_payload(),
            status=OutboxStatus.PENDING,
            next_attempt_at=datetime.now(UTC),
        )
        session.add(row)

    assert await deliver_pending(session_factory) == 1
    assert len(received) == 1
    assert received[0][0] == event
    assert received[0][1] == {"target": "test"}
