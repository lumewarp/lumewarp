from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lumewarp_control_plane.db import SessionFactory, create_session_factory
from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.models import (
    NotifierChannel,
    NotifierOutbox,
    OutboxStatus,
    ScopeType,
    Team,
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
async def test_webhook_outbox_retries_and_marks_delivered(
    session_factory: SessionFactory, monkeypatch
):
    server, url, state = await _webhook_server()
    monkeypatch.setenv("LUMEWARP_TEST_WEBHOOK_URL", url)
    team_id = uuid7()
    event = BudgetEvent(
        kind="soft",
        scope_type=ScopeType.TEAM,
        scope_id=team_id,
        team_id=team_id,
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=80,
        budget_nano_usd=100,
    )
    async with session_factory() as session, session.begin():
        session.add(Team(id=team_id, name=f"notifier-team-{uuid7().hex}"))
        await session.flush()
        channel = NotifierChannel(
            name=f"webhook-{uuid7().hex}",
            kind="webhook",
            team_id=team_id,
            config={"url_ref": "LUMEWARP_TEST_WEBHOOK_URL"},
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
        assert b'"scope_type":"team"' in state["bodies"][1]
    finally:
        server.close()
        await server.wait_closed()


async def _webhook_row(factory: SessionFactory, config: dict) -> int:
    team_id = uuid7()
    async with factory() as session, session.begin():
        session.add(Team(id=team_id, name=f"webhook-team-{uuid7().hex}"))
        await session.flush()
        channel = NotifierChannel(
            name=f"webhook-{uuid7().hex}", kind="webhook", team_id=team_id, config=config
        )
        session.add(channel)
        await session.flush()
        event = BudgetEvent(
            kind="soft",
            scope_type=ScopeType.TEAM,
            scope_id=team_id,
            team_id=team_id,
            window_start=datetime(2026, 10, 1, tzinfo=UTC),
            spent_nano_usd=1,
            budget_nano_usd=2,
        )
        row = NotifierOutbox(
            dedupe_key=f"webhook:{uuid7()}",
            channel_id=channel.id,
            event_type="budget.soft",
            payload=event.to_payload(),
            status=OutboxStatus.PENDING,
            next_attempt_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return row.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {},  # no reference at all
        {"url": "https://hooks.example.test/budget"},  # the old inline shape
        {"url_ref": "LUMEWARP_TEST_HOOK", "url": "https://hooks.example.test/budget"},
        {"url_ref": "LUMEWARP_TEST_HOOK", "token": "lw-secret"},  # unclassified key
        {"url_ref": ""},
    ],
)
async def test_webhook_config_refuses_anything_but_an_env_reference(
    session_factory: SessionFactory, config
):
    with pytest.raises(IntegrityError):
        await _webhook_row(session_factory, config)


@pytest.mark.asyncio
async def test_webhook_url_env_is_read_at_delivery_time(
    session_factory: SessionFactory, monkeypatch
):
    reference = "LUMEWARP_TEST_WEBHOOK_ENV"
    monkeypatch.delenv(reference, raising=False)
    row_id = await _webhook_row(session_factory, {"url_ref": reference})
    assert await deliver_pending(session_factory) == 0
    async with session_factory() as session, session.begin():
        row = await session.get(NotifierOutbox, row_id)
        assert row.last_error == "LookupError"
        assert row.attempts == 1
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    # The value is validated when resolved: a non-HTTP URL is an error, not a request.
    monkeypatch.setenv(reference, "file:///etc/passwd")
    assert await deliver_pending(session_factory) == 0
    async with session_factory() as session:
        row = await session.get(NotifierOutbox, row_id)
        assert row.last_error == "ValueError"


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
    team_id = uuid7()
    event = BudgetEvent(
        kind="hard",
        scope_type=ScopeType.SERVICE_ACCOUNT,
        scope_id=uuid7(),
        team_id=team_id,
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=100,
        budget_nano_usd=100,
    )
    async with session_factory() as session, session.begin():
        session.add(Team(id=team_id, name=f"plugin-team-{uuid7().hex}"))
        await session.flush()
        channel = NotifierChannel(
            name=f"plugin-{uuid7().hex}",
            kind="test-notifier",
            team_id=team_id,
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


def _plugin(monkeypatch, send):
    from lumewarp_control_plane import plugins

    class TestPlugin:
        api_version = plugins.PLUGIN_API_VERSION

    TestPlugin.send = send

    class Loaded:
        name = "test-notifier"
        plugin = TestPlugin()

    monkeypatch.setattr(
        plugins, "load", lambda group: [Loaded()] if group == plugins.NOTIFIER else []
    )


async def _plugin_rows(factory: SessionFactory, count: int, **row_values) -> list[int]:
    team_id = uuid7()
    ids = []
    async with factory() as session, session.begin():
        session.add(Team(id=team_id, name=f"plugin-team-{uuid7().hex}"))
        await session.flush()
        channel = NotifierChannel(
            name=f"plugin-{uuid7().hex}", kind="test-notifier", team_id=team_id, config={}
        )
        session.add(channel)
        await session.flush()
        base = datetime.now(UTC) - timedelta(seconds=10)
        for i in range(count):
            event = BudgetEvent(
                kind="soft",
                scope_type=ScopeType.TEAM,
                scope_id=team_id,
                team_id=team_id,
                window_start=datetime(2026, 10, 1, tzinfo=UTC),
                spent_nano_usd=i,
                budget_nano_usd=100,
            )
            row = NotifierOutbox(
                dedupe_key=f"plugin:{uuid7()}",
                channel_id=channel.id,
                event_type="budget.soft",
                payload=event.to_payload(),
                status=OutboxStatus.PENDING,
                next_attempt_at=base + timedelta(milliseconds=i),
                **row_values,
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
    return ids


@pytest.mark.asyncio
async def test_interrupted_batch_keeps_earlier_deliveries(
    session_factory: SessionFactory, monkeypatch
):
    calls = []

    async def send(self, event, config):
        calls.append(event.spent_nano_usd)
        if len(calls) == 2:
            raise asyncio.CancelledError  # shutdown mid-batch

    _plugin(monkeypatch, send)
    first, second, third = await _plugin_rows(session_factory, 3)
    with pytest.raises(asyncio.CancelledError):
        await deliver_pending(session_factory)

    async with session_factory() as session:
        rows = {i: await session.get(NotifierOutbox, i) for i in (first, second, third)}
    assert rows[first].status is OutboxStatus.DONE
    assert rows[first].delivered_at is not None
    # Interrupted and not-yet-tried rows stay pending under a lease, not re-sent at once.
    for row_id in (second, third):
        assert rows[row_id].status is OutboxStatus.PENDING
        assert rows[row_id].attempts == 0
        assert rows[row_id].next_attempt_at > datetime.now(UTC)
    assert await deliver_pending(session_factory) == 0
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_delivery_runs_outside_a_database_transaction(
    session_factory: SessionFactory, monkeypatch
):
    from sqlalchemy import text

    observed = []

    async def send(self, event, config):
        async with session_factory() as session:
            # Another connection can lock the row: the worker holds no lock while sending.
            async with session.begin():
                locked = await session.execute(
                    text("SELECT id FROM notifier_outbox WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": row_id},
                )
                observed.append(locked.scalar_one())

    _plugin(monkeypatch, send)
    (row_id,) = await _plugin_rows(session_factory, 1)
    assert await deliver_pending(session_factory) == 1
    assert observed == [row_id]


@pytest.mark.asyncio
async def test_failing_row_becomes_failed_after_max_attempts(
    session_factory: SessionFactory, monkeypatch
):
    from lumewarp_control_plane import notifier

    async def send(self, event, config):
        raise RuntimeError("failed to POST https://hooks.example.test/secret-webhook-token")

    _plugin(monkeypatch, send)
    (row_id,) = await _plugin_rows(session_factory, 1, attempts=notifier._MAX_ATTEMPTS - 1)
    assert await deliver_pending(session_factory) == 0
    async with session_factory() as session:
        row = await session.get(NotifierOutbox, row_id)
    assert row.status is OutboxStatus.FAILED
    assert row.attempts == notifier._MAX_ATTEMPTS
    # Only the exception class: messages can carry URLs and secrets.
    assert row.last_error == "RuntimeError"


@pytest.mark.asyncio
async def test_webhook_total_timeout_bounds_slow_drip_server():
    from lumewarp_control_plane.notifier import WebhookNotifier

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        try:
            for _ in range(100):  # each byte arrives well within the per-phase timeout
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.05)
        except ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    event = BudgetEvent(
        kind="soft",
        scope_type=ScopeType.TEAM,
        scope_id=uuid7(),
        team_id=uuid7(),
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        spent_nano_usd=1,
        budget_nano_usd=1,
    )
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await WebhookNotifier(f"http://127.0.0.1:{port}/", 0.3).notify(event)
        assert asyncio.get_running_loop().time() - started < 1.5
        with pytest.raises(ValueError):
            WebhookNotifier("https://example.test/", 31)
    finally:
        server.close()
        await server.wait_closed()
