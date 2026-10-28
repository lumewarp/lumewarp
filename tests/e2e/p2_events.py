"""Commit notifications in the disposable e2e database, not timing-based polling."""

import asyncio
import concurrent.futures
import threading
import time
from contextlib import AbstractContextManager

import asyncpg


class DatabaseEvents(AbstractContextManager):
    def __init__(self, dsn):
        self.dsn = dsn
        self.condition = threading.Condition()
        self.generation = 0
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        async def listen():
            conn = await asyncpg.connect(self.dsn)
            try:
                await conn.execute("""
                    CREATE FUNCTION e2e_changed() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN PERFORM pg_notify('e2e_changed', TG_TABLE_NAME); RETURN NULL; END $$;
                """)
                for table in ("usage_events", "config_versions", "gateway_sessions", "api_keys"):
                    await conn.execute(
                        f"CREATE TRIGGER e2e_changed AFTER INSERT OR UPDATE ON {table} "
                        "FOR EACH STATEMENT EXECUTE FUNCTION e2e_changed()"
                    )
                await conn.add_listener("e2e_changed", self.changed)
                self.loop = asyncio.get_running_loop()
                self.stop = asyncio.Event()
                self.ready.set_result(None)
                await self.stop.wait()
            finally:
                await conn.close()

        try:
            asyncio.run(listen())
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            else:
                self.error = exc
                self.changed()

    def changed(self, *args):
        with self.condition:
            self.generation += 1
            self.condition.notify_all()

    def __enter__(self):
        self.thread.start()
        self.ready.result(timeout=10)
        return self

    def wait(self, predicate, *, timeout=10, what="database state"):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if getattr(self, "error", None):
                    raise self.error
                value = predicate()
                if value:
                    return value
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"timed out waiting for {what}"
                assert self.condition.wait(remaining), f"timed out waiting for {what}"

    def __exit__(self, *args):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "database listener did not stop"
