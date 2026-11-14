"""Shared rate limits with the real control plane, Redis and three gateways.

docs/operations/gateway.md, "Shared rate limits": a background sync pushes each
gateway's per-minute counts and in-flight gauge to one Redis namespace, so rpm,
tpm and max_parallel hold across every gateway instead of once per process. When
Redis is unreachable a gateway counts locally (N x), and a killed gateway's
parallel slots free after the 3 s staleness window.

These tests need a Redis. `scripts/e2e-core.sh` starts a throwaway
`redis:7-alpine` with docker when `E2E_REDIS_URL` is unset and docker is
available; the CI e2e job gets a `redis:7` service. Without the variable the
module skips, because no other gate needs one.
"""

import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO
from urllib.parse import urlsplit

import pytest
from stack import free_port, run_id, wait_for

REDIS_URL = os.environ.get("E2E_REDIS_URL", "").strip()
# The design's lower bound, so the tests also cover the fast sync path.
SYNC_MS = 20
GATEWAYS = 3
# The documented over-admission bound per window: the limit plus one share per
# gateway (limit + n), from rounding and from an gateway that joined before
# the others saw it.
OVER_LIMIT = GATEWAYS

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="E2E_REDIS_URL is not set: shared limits need a Redis "
    "(scripts/e2e-core.sh starts one with docker when available; CI sets it)",
)


# Redis helpers -------------------------------------------------------------------


class RedisClient:
    """A tiny RESP client: PING and HGETALL are all these tests need.

    It is deliberately independent of the gateway: the tests read the in-flight
    hash the design's sync script writes (and verify the gateway is alive) and
    never touch Redis from the request path.
    """

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 6379
        self.password = parts.password
        self.db = parts.path.strip("/") or "0"

    def _open(self) -> tuple[socket.socket, BinaryIO]:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        reader = sock.makefile("rb")
        try:
            if self.password:
                self._call(sock, reader, "AUTH", self.password)
            if self.db != "0":
                self._call(sock, reader, "SELECT", self.db)
        except Exception:
            _close(sock)
            reader.close()
            raise
        return sock, reader

    @staticmethod
    def _call(sock: socket.socket, reader, *args: str):
        sock.sendall(_encode(args))
        return _read_reply(reader)

    def command(self, *args: str):
        sock, reader = self._open()
        try:
            return self._call(sock, reader, *args)
        finally:
            _close(sock)
            reader.close()


def _encode(args: tuple[str, ...]) -> bytes:
    out = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        data = str(arg).encode()
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def _read_reply(reader):
    line = reader.readline()
    if not line.endswith(b"\r\n"):
        raise RuntimeError("short Redis reply")
    kind, body = line[:1], line[1:-2]
    if kind == b"+":
        return body.decode()
    if kind == b"-":
        raise RuntimeError(f"Redis error: {body.decode()}")
    if kind == b":":
        return int(body)
    if kind == b"$":
        length = int(body)
        if length < 0:
            return None
        data = reader.read(length + 2)
        if len(data) != length + 2 or not data.endswith(b"\r\n"):
            raise RuntimeError("truncated Redis bulk string")
        return data[:-2].decode()
    if kind == b"*":
        count = int(body)
        if count < 0:
            return None
        return [_read_reply(reader) for _ in range(count)]
    raise RuntimeError(f"unexpected Redis reply {line!r}")


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


@pytest.fixture(scope="session")
def redis() -> RedisClient:
    """Prove E2E_REDIS_URL works; a stale URL is a failure, not a skip."""
    client = RedisClient(REDIS_URL)
    try:
        reply = client.command("PING")
    except (OSError, RuntimeError) as error:
        pytest.fail(f"E2E_REDIS_URL is set but unreachable: {error}")
    if reply != "PONG":
        pytest.fail(f"E2E_REDIS_URL answered {reply!r} to PING")
    return client


class RedisProxy:
    """A loopback TCP pass-through the test can close, to make Redis look down.

    Closing the listener and every accepted socket gives the gateways a refused
    connection (what a stopped Redis looks like), and opening it again lets the
    sync task's next reconnect succeed. It never touches the Redis data, so the
    outage only changes reachability.
    """

    def __init__(self, host: str, port: int) -> None:
        self.upstream = (host, port)
        self.port = free_port()
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._sockets: list[socket.socket] = []
        self.open()

    def open(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(128)
        listener.settimeout(0.2)
        with self._lock:
            self._listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def close(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
            sockets, self._sockets = self._sockets, []
        if listener is not None:
            _close(listener)
        for sock in sockets:
            _close(sock)

    def _accept(self, listener: socket.socket) -> None:
        while True:
            with self._lock:
                if self._listener is not listener:
                    return
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.upstream, timeout=2)
            except OSError:
                _close(client)
                continue
            client.settimeout(None)
            upstream.settimeout(None)
            with self._lock:
                if self._listener is not listener:
                    _close(client)
                    _close(upstream)
                    return
                self._sockets.extend((client, upstream))
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            _close(source)
            _close(sink)


# Stack helpers -------------------------------------------------------------------


def _namespace(tag: str) -> str:
    return f"e2e-{run_id()}-{tag}-{uuid.uuid4().hex[:6]}"


def _publish_key_limits(stack, key, **limits) -> int:
    """Set limits on a key and return the first config version that has them."""
    before = stack.current_version()
    stack.ok("PATCH", f"/keys/{key.id}", json=limits)

    def published():
        version = stack.current_version()
        return version if version > before else None

    return wait_for(published, 10, "a config version with the new key limits")


def _connected(stack) -> list[dict]:
    return [gateway for gateway in stack.gateway_items() if gateway["connected"]]


def _start_gateways(stack, redis_url: str, namespace: str, tag: str) -> list:
    """Three gateway processes, one token, one Redis namespace."""
    gateways = []
    for index in range(1, GATEWAYS + 1):
        hostname = f"gw-{tag}-{index}"
        gateways.append(
            stack.start_gateway(
                state_dir=stack.dir / f"state-{hostname}",
                tag=hostname,
                extra_env={
                    "HOSTNAME": hostname,
                    "LUMEWARP_SHARED_LIMITS_REDIS_URL": redis_url,
                    "LUMEWARP_SHARED_LIMITS_SYNC_MS": str(SYNC_MS),
                    "LUMEWARP_SHARED_LIMITS_NAMESPACE": namespace,
                },
            )
        )
    return gateways


def _wait_gateways(stack, namespace: str) -> list[dict]:
    def ready():
        gateways = _connected(stack)
        return len(gateways) == GATEWAYS and all(
            gateway["shared_limits_enabled"] and gateway["shared_limits_namespace"] == namespace
            for gateway in gateways
        )

    wait_for(ready, 20, f"{GATEWAYS} connected gateways sharing {namespace}")
    return _connected(stack)


def _wait_acked(stack, version: int) -> None:
    def acked():
        gateways = _connected(stack)
        return len(gateways) == GATEWAYS and all(
            (gateway["acked_version"] or 0) >= version for gateway in gateways
        )

    wait_for(acked, 20, f"{GATEWAYS} gateways ACKed a version >= {version}")


def _wait_fallback(gateways, value: int, timeout: float, what: str) -> None:
    def reached():
        return all(
            gateway.metric("lumewarp_shared_limits_fallback") == float(value)
            for gateway in gateways
        )

    wait_for(reached, timeout, what, interval=0.02)


def _series(gateway, name: str) -> dict[str, float]:
    out = {}
    for line in gateway.metrics().splitlines():
        if line.startswith(name + "{"):
            labels, value = line.rsplit(" ", 1)
            out[labels.removeprefix(name)] = float(value)
    return out


def _inflight(client: RedisClient, namespace: str, scope: str) -> dict[str, int]:
    base = f"{{lw:{namespace}}}:{scope}:p"
    flat = client.command("HGETALL", base)
    values = {}
    for field, value in zip(flat[::2], flat[1::2], strict=False):
        count, _, _stamp = value.partition(":")
        values[field] = int(count)
    return values


def _retry_after(response) -> int:
    value = response.headers.get("retry-after")
    assert value is not None, (response.status_code, response.text)
    seconds = int(value)
    assert 1 <= seconds <= 60, (seconds, response.text)
    return seconds


# Tests ---------------------------------------------------------------------------


def test_rpm_limit_holds_across_three_gateways(stack, redis):
    """18 requests round-robin over three gateways admit at most limit + n.

    The gateways started by this test (and the following ones) are not the
    stack's default gateway: three processes share the token, each with its own
    state dir and generated gateway id.
    """
    namespace = _namespace("rpm")
    seeded = stack.seed()
    version = _publish_key_limits(stack, seeded.a, rpm_limit=12)
    gateways = _start_gateways(stack, REDIS_URL, namespace, "rpm")

    rows = _wait_gateways(stack, namespace)
    gateway_ids = {row["gateway_id"] for row in rows}
    assert len(gateway_ids) == GATEWAYS, gateway_ids
    assert any(name.startswith("gw-rpm-1-") for name in gateway_ids), gateway_ids
    assert any(name.startswith("gw-rpm-2-") for name in gateway_ids), gateway_ids
    assert any(name.startswith("gw-rpm-3-") for name in gateway_ids), gateway_ids
    for row in rows:
        assert row["shared_limits_enabled"] is True
        assert row["shared_limits_namespace"] == namespace
    _wait_acked(stack, version)
    # The first successful sync clears the startup fallback, so both halves of
    # the counter are shared before the burst.
    _wait_fallback(gateways, 0, 10, "shared mode on every gateway")

    responses = []
    for index in range(18):
        responses.append(gateways[index % GATEWAYS].chat(seeded.a.secret))
        # A sync between two requests of one gateway keeps the views fresh;
        # this is the documented steady state, not a stress test.
        time.sleep(0.02)

    admitted = [response for response in responses if response.status_code == 200]
    limited = [response for response in responses if response.status_code == 429]
    assert len(admitted) + len(limited) == len(responses)
    print(f"rpm 12: {len(admitted)} of 18 admitted across 3 gateways")
    assert 12 <= len(admitted) <= 12 + OVER_LIMIT, [response.status_code for response in responses]
    assert limited, "18 requests with a limit of 12 must hit the limit"
    for response in limited:
        assert response.json()["error"]["code"] == "rate_limit_exceeded"
        _retry_after(response)


def test_max_parallel_holds_across_three_gateways(stack, redis):
    """Concurrent requests over three gateways never exceed limit + n in flight."""
    namespace = _namespace("parallel")
    seeded = stack.seed()
    slow_port = stack.start_mock("slow-parallel", latency_ms=1500)
    upstream = stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "slow",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{slow_port}/v1",
        },
    )
    model = stack.ok(
        "POST",
        "/models",
        json={
            "name": "slow-a",
            "upstream_id": upstream["id"],
            "upstream_model": "mock",
            "model_type": "llm",
        },
    )
    stack.ok(
        "POST",
        "/routers",
        json={"name": "slow", "members": [{"model_id": model["id"]}]},
    )
    version = _publish_key_limits(stack, seeded.a, max_parallel=3)
    gateways = _start_gateways(stack, REDIS_URL, namespace, "parallel")
    _wait_gateways(stack, namespace)
    _wait_acked(stack, version)
    _wait_fallback(gateways, 0, 10, "shared mode on every gateway")

    # One request per gateway teaches all three the scope (every gateway then
    # writes its in-flight entry on each sync), so the shares are divided by 3
    # before the measured burst.
    with ThreadPoolExecutor(max_workers=GATEWAYS) as pool:
        warm = [pool.submit(gateway.chat, seeded.a.secret, model="slow") for gateway in gateways]
        assert [future.result(timeout=10).status_code for future in warm] == [200] * GATEWAYS

    # The warm-up requests held every slot, so each gateway's last sync divided
    # a full gauge: wait until they are all released and the next syncs have
    # redivided the headroom before measuring.
    scope = f"key:{seeded.a.id}"
    wait_for(
        lambda: (
            len(_inflight(redis, namespace, scope)) == GATEWAYS
            and all(count == 0 for count in _inflight(redis, namespace, scope).values())
        ),
        5,
        "the warm-up slots are released",
        interval=0.02,
    )
    time.sleep(3 * SYNC_MS / 1000)

    burst = 12

    def burst_once() -> list[tuple[float, float, object]]:
        barrier = threading.Barrier(burst)
        results: list[tuple[float, float, object]] = []
        lock = threading.Lock()

        def call(index: int) -> None:
            gateway = gateways[index % GATEWAYS]
            barrier.wait()
            started = time.monotonic()
            response = gateway.chat(seeded.a.secret, model="slow")
            finished = time.monotonic()
            with lock:
                results.append((started, finished, response))

        with ThreadPoolExecutor(max_workers=burst) as pool:
            list(pool.map(call, range(burst)))
        return results

    # A share is redivided at a sync, so on a stalled tick every request can be
    # refused before any slot is taken; that is a transient, not the limit, so
    # retry once the sync task has had a few intervals.
    for _ in range(3):
        results = burst_once()
        admitted = [result for result in results if result[2].status_code == 200]
        if admitted:
            break
        time.sleep(3 * SYNC_MS / 1000)

    limited = [result for result in results if result[2].status_code == 429]
    assert admitted, [result[2].status_code for result in results]
    assert len(admitted) <= 3 + OVER_LIMIT, [result[2].status_code for result in results]
    assert limited, "12 concurrent requests with max_parallel 3 must be limited"
    assert len(admitted) + len(limited) == burst, [result[2].status_code for result in results]
    for response in (result[2] for result in limited):
        assert response.json()["error"]["code"] == "rate_limit_exceeded"
        _retry_after(response)
    # Every admitted request really ran against the slow upstream, so its
    # client-side interval is (a lower bound of) the slot it held.
    for started, finished, _response in admitted:
        assert finished - started > 1.0, finished - started

    # The peak number of admitted requests in flight together. Intervals are
    # sorted with the end before the start at equal timestamps, which can only
    # over-count overlaps, never hide one.
    events = sorted(
        [(started, 1) for started, _finished, _response in admitted]
        + [(finished, -1) for _started, finished, _response in admitted],
        key=lambda event: (event[0], event[1]),
    )
    peak = current = 0
    for _at, delta in events:
        current += delta
        peak = max(peak, current)
    print(f"max_parallel 3: {len(admitted)} admitted of {burst}, peak {peak} in flight")
    assert peak <= 3 + OVER_LIMIT, peak
    assert peak >= 2, "the burst did not overlap; the test proves nothing"

    # Once the burst's slots are released, the key is admitted again. A sync
    # redivides the share after the release, so retry instead of assuming it.
    def admitted_again() -> bool:
        return gateways[0].chat(seeded.a.secret, model="slow").status_code == 200

    wait_for(admitted_again, 5, "a slot after the burst was released")


def test_redis_outage_falls_back_to_per_gateway_limits_and_recovers(stack, redis):
    """Redis down: fallback metric, N x limits; Redis back: sharing resumes."""
    namespace = _namespace("fallback")
    seeded = stack.seed()
    version = _publish_key_limits(stack, seeded.a, rpm_limit=12)
    parts = urlsplit(REDIS_URL)
    proxy = RedisProxy(parts.hostname or "127.0.0.1", parts.port or 6379)
    try:
        database = parts.path or "/0"
        gateways = _start_gateways(
            stack, f"redis://127.0.0.1:{proxy.port}{database}", namespace, "fallback"
        )
        _wait_gateways(stack, namespace)
        _wait_acked(stack, version)
        _wait_fallback(gateways, 0, 10, "shared mode on every gateway")

        proxy.close()
        closed_at = time.monotonic()
        _wait_fallback(gateways, 1, 5, "fallback on every gateway")
        entered = time.monotonic() - closed_at
        print(f"fallback entered {entered:.2f}s after Redis became unreachable")
        # The task enters fallback after 1 s without a successful sync.
        assert entered <= 2.5, entered
        assert any(
            _series(gateway, "lumewarp_shared_limits_sync_errors_total").get(
                '{reason="connect"}', 0
            )
            > 0
            for gateway in gateways
        ), "a refused connection must be counted as a connect error"

        # Each gateway counts alone at the full limit: 10 per gateway, so all
        # 30 requests are served even though the shared limit is 12.
        statuses = [
            gateways[index % GATEWAYS].chat(seeded.a.secret).status_code for index in range(30)
        ]
        assert statuses == [200] * 30, statuses

        proxy.open()
        _wait_fallback(gateways, 0, 5, "shared mode after Redis returned")

        # The counts admitted in fallback are now in Redis, so the whole key is
        # over its limit again: every gateway answers 429, not 200.
        def all_limited() -> bool:
            return all(gateway.chat(seeded.a.secret).status_code == 429 for gateway in gateways)

        wait_for(all_limited, 5, "shared counting after recovery", interval=0.1)
        _retry_after(gateways[0].chat(seeded.a.secret))
    finally:
        proxy.close()


def test_killed_gateway_frees_its_parallel_slots(stack, redis, request):
    """SIGKILL with slots held: the others admit again after the 3 s stale window."""
    namespace = _namespace("kill")
    seeded = stack.seed()
    slow_port = stack.start_mock("slow-kill", latency_ms=5000)
    upstream = stack.ok(
        "POST",
        "/upstreams",
        json={
            "name": "slow",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{slow_port}/v1",
        },
    )
    model = stack.ok(
        "POST",
        "/models",
        json={
            "name": "slow-a",
            "upstream_id": upstream["id"],
            "upstream_model": "mock",
            "model_type": "llm",
        },
    )
    stack.ok(
        "POST",
        "/routers",
        json={"name": "slow", "members": [{"model_id": model["id"]}]},
    )
    version = _publish_key_limits(stack, seeded.a, max_parallel=3)
    gateways = _start_gateways(stack, REDIS_URL, namespace, "kill")
    victim, survivor = gateways[0], gateways[1]
    rows = _wait_gateways(stack, namespace)
    _wait_acked(stack, version)
    _wait_fallback(gateways, 0, 10, "shared mode on every gateway")
    victim_id = next(
        row["gateway_id"] for row in rows if row["gateway_id"].startswith("gw-kill-1-")
    )
    survivor_id = next(
        row["gateway_id"] for row in rows if row["gateway_id"].startswith("gw-kill-2-")
    )
    third_id = next(
        row["gateway_id"] for row in rows if row["gateway_id"].startswith("gw-kill-3-")
    )

    # One slow request per gateway: every gateway's 20 ms sync catches the
    # in-flight slot, so all three learn the scope and keep publishing their
    # entry (including zero) from then on. A fast warm-up can miss every sync
    # window and leave a gateway outside the shared gauge.
    with ThreadPoolExecutor(max_workers=GATEWAYS) as pool:
        warm = [pool.submit(gateway.chat, seeded.a.secret, model="slow") for gateway in gateways]
        assert [future.result(timeout=15).status_code for future in warm] == [200] * GATEWAYS
    scope = f"key:{seeded.a.id}"
    wait_for(
        lambda: set(_inflight(redis, namespace, scope)) == {victim_id, survivor_id, third_id},
        5,
        "every gateway publishes its in-flight entry",
        interval=0.02,
    )
    # The warm-up held every slot, so the last sync divided zero headroom: give
    # the sync task a few intervals to divide it again before taking the slots.
    time.sleep(3 * SYNC_MS / 1000)

    # Take the slots one at a time: one gateway may take one slot per sync
    # (its share is the headroom divided by three), so three requests fired at
    # once would leave two rejected. Each admitted request holds for 5 s.
    held = ThreadPoolExecutor(max_workers=3)
    hold_stop = threading.Event()
    futures = []

    def hold_one():
        while not hold_stop.is_set():
            response = victim.chat(seeded.a.secret, model="slow")
            if response.status_code == 200:
                return response
            assert response.status_code == 429, response.text
            time.sleep(SYNC_MS / 1000)

    # A failing assertion above the probe block must not leave a retry loop
    # running into the interpreter's exit join.
    def stop_holding() -> None:
        hold_stop.set()
        held.shutdown(wait=False)

    request.addfinalizer(stop_holding)

    for count in (1, 2, 3):
        futures.append(held.submit(hold_one))
        wait_for(
            lambda count=count: _inflight(redis, namespace, scope).get(victim_id) == count,
            5,
            f"the victim holds {count} slots",
            interval=0.02,
        )
    # Give the survivors a few sync intervals to see the three slots before the
    # kill; the victim keeps holding them (5 s upstream latency) meanwhile.
    time.sleep(0.1)
    # All three must still be held at the kill (the slow mock answers after 5 s).
    assert _inflight(redis, namespace, scope).get(victim_id) == 3

    victim.proc.kill()
    killed_at = time.monotonic()
    # The killed process never released them, so the survivors still count the
    # slots from Redis until they are stale.
    blocked = survivor.chat(seeded.a.secret, model="slow")
    assert blocked.status_code == 429, blocked.text
    _retry_after(blocked)

    # Probe the survivor until it admits; the Redis in-flight entry of the
    # survivor appears within one sync of the admission, which gives the
    # admission time without waiting for the slow response.
    admitted_at: list[float] = []
    probe_statuses: list[int] = []
    probe_stop = threading.Event()

    def probe() -> None:
        while not probe_stop.is_set():
            response = survivor.chat(seeded.a.secret, model="slow")
            probe_statuses.append(response.status_code)
            if response.status_code == 200:
                return
            time.sleep(0.05)

    prober = threading.Thread(target=probe, daemon=True)
    prober.start()
    try:
        wait_for(
            lambda: _inflight(redis, namespace, scope).get(survivor_id, 0) > 0,
            8,
            "the survivor admits again",
            interval=0.02,
        )
        admitted_at.append(time.monotonic() - killed_at)
    finally:
        probe_stop.set()
        prober.join(timeout=15)

    assert admitted_at, "the survivor never admitted"
    assert probe_statuses[-1:] == [200], probe_statuses
    assert set(probe_statuses[:-1]) <= {429}, probe_statuses
    freed = admitted_at[0]
    print(f"parallel slots freed {freed:.2f}s after SIGKILL")
    # The script ignores (and deletes) in-flight fields older than 3 s; one
    # sync interval of slack plus scheduling keeps this under 4 s.
    assert 2.5 <= freed <= 4.0, freed

    # Whatever the held requests did, they cannot have completed: the process
    # they were sent to was killed mid-call.
    for future in futures:
        try:
            future.result(timeout=5)
        except Exception:  # noqa: BLE001, S110 - the point is that it did not answer
            pass
        else:
            raise AssertionError("a request to the killed gateway completed")

    wait_for(
        lambda: len(_connected(stack)) == 2,
        15,
        "the control plane dropped the killed gateway",
    )
