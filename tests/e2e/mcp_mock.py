"""A typed client for `bench/mock-mcp`, the controllable MCP upstream.

The binary serves any number of named instances at `/s/<name>/mcp` and an admin
API at `/admin/*`. This module starts one process on a free port, exposes the
small typed surface the tests use, and stops the whole process group on exit.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import httpx

ROOT = Path(__file__).resolve().parents[2]
MCP_MOCK_BIN = Path(
    os.environ.get("E2E_MCP_MOCK_BIN", ROOT / "bench/mock-mcp/target/release/mock-mcp")
)
# Never collide with the LLM benchmark's fixed ports.
FORBIDDEN_PORTS = {9000, 9100, 4100, 4101, 5432, 8080, 8081}


def free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port


@dataclass(frozen=True)
class Fault:
    kind: str
    bytes: int | None = None
    events: int | None = None
    interval_ms: int | None = None
    after_events: int | None = None
    status: int | None = None
    content_type: str | None = None
    www_authenticate: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> Fault | None:
        if not raw:
            return None
        return cls(
            kind=raw["kind"],
            bytes=raw.get("bytes"),
            events=raw.get("events"),
            interval_ms=raw.get("interval_ms"),
            after_events=raw.get("after_events"),
            status=raw.get("status"),
            content_type=raw.get("content_type"),
            www_authenticate=raw.get("www_authenticate"),
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        for name in (
            "bytes",
            "events",
            "interval_ms",
            "after_events",
            "status",
            "content_type",
            "www_authenticate",
        ):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


@dataclass(frozen=True)
class ErrorSpec:
    code: int | None = None
    message: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> ErrorSpec | None:
        if not raw:
            return None
        return cls(code=raw.get("code"), message=raw.get("message"))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.code is not None:
            out["code"] = self.code
        if self.message is not None:
            out["message"] = self.message
        return out


DEFAULT_INPUT_SCHEMA = {"type": "object"}


@dataclass(frozen=True)
class Tool:
    """One tool definition plus the behaviour its call should have."""

    name: str
    behaviour: str = "echo"
    description: str | None = None
    input_schema: dict[str, Any] = field(
        default_factory=lambda: dict(DEFAULT_INPUT_SCHEMA)
    )
    definition: dict[str, Any] | None = None
    result: Any = None
    error: ErrorSpec | None = None
    sleep_ms: int = 0
    progress_events: int = 0
    progress_interval_ms: int = 0
    response: str | None = None
    fault: Fault | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Tool:
        return cls(
            name=raw["name"],
            behaviour=raw.get("behaviour", "echo"),
            description=raw.get("description"),
            input_schema=raw.get("input_schema") or dict(DEFAULT_INPUT_SCHEMA),
            definition=raw.get("definition"),
            result=raw.get("result"),
            error=ErrorSpec.from_json(raw.get("error")),
            sleep_ms=raw.get("sleep_ms", 0),
            progress_events=raw.get("progress_events", 0),
            progress_interval_ms=raw.get("progress_interval_ms", 0),
            response=raw.get("response"),
            fault=Fault.from_json(raw.get("fault")),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "behaviour": self.behaviour,
            "description": self.description,
            "input_schema": self.input_schema,
            "definition": self.definition,
            "result": self.result,
            "error": self.error.to_json() if self.error else None,
            "sleep_ms": self.sleep_ms,
            "progress_events": self.progress_events,
            "progress_interval_ms": self.progress_interval_ms,
            "response": self.response,
            "fault": self.fault.to_json() if self.fault else None,
        }


@dataclass(frozen=True)
class Server:
    """An instance to create. `bearer_token` turns upstream auth on."""

    name: str
    era: str = "modern"
    response: str = "json"
    bearer_token: str | None = None
    allowed_origins: tuple[str, ...] = ()
    ttl_ms: int = 60_000
    cache_scope: str = "public"
    session: str = "optional"
    get_stream: bool = False
    instructions: str | None = None
    tools: tuple[Tool, ...] = ()
    fault: Fault | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "era": self.era,
            "response": self.response,
            "auth": (
                {"kind": "bearer", "token": self.bearer_token}
                if self.bearer_token
                else {"kind": "none"}
            ),
            "allowed_origins": list(self.allowed_origins),
            "ttl_ms": self.ttl_ms,
            "cache_scope": self.cache_scope,
            "session": self.session,
            "get_stream": self.get_stream,
            "instructions": self.instructions,
            "tools": [tool.to_json() for tool in self.tools],
            "fault": self.fault.to_json() if self.fault else None,
        }


@dataclass(frozen=True)
class ServerConfig:
    name: str
    era: str
    response: str
    auth: dict[str, Any]
    allowed_origins: tuple[str, ...]
    ttl_ms: int
    cache_scope: str
    session: str
    get_stream: bool
    instructions: str | None
    tools: tuple[Tool, ...]
    fault: Fault | None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ServerConfig:
        return cls(
            name=raw["name"],
            era=raw["era"],
            response=raw["response"],
            auth=raw["auth"],
            allowed_origins=tuple(raw.get("allowed_origins", ())),
            ttl_ms=raw["ttl_ms"],
            cache_scope=raw["cache_scope"],
            session=raw["session"],
            get_stream=raw["get_stream"],
            instructions=raw.get("instructions"),
            tools=tuple(Tool.from_json(tool) for tool in raw.get("tools", ())),
            fault=Fault.from_json(raw.get("fault")),
        )

    def tool(self, name: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.name == name), None)


@dataclass(frozen=True)
class RequestRecord:
    """One request exactly as the upstream saw it."""

    seq: int
    server: str
    http_method: str
    path: str
    headers: dict[str, tuple[str, ...]]
    body: str
    body_truncated: bool
    method: str | None
    request_id: Any
    tool: str | None
    session_id: str | None
    era: str | None
    status: int | None
    content_type: str | None
    outcome: str | None
    result_type: str | None
    cancelled: bool
    cancelled_by: str | None
    disconnected: bool
    completed_after_disconnect: bool
    fault: str | None
    received_at_ms: int
    completed_at_ms: int | None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> RequestRecord:
        return cls(
            seq=raw["seq"],
            server=raw["server"],
            http_method=raw["http_method"],
            path=raw["path"],
            headers={name: tuple(values) for name, values in raw["headers"].items()},
            body=raw["body"],
            body_truncated=raw.get("body_truncated", False),
            method=raw.get("method"),
            request_id=raw.get("request_id"),
            tool=raw.get("tool"),
            session_id=raw.get("session_id"),
            era=raw.get("era"),
            status=raw.get("status"),
            content_type=raw.get("content_type"),
            outcome=raw.get("outcome"),
            result_type=raw.get("result_type"),
            cancelled=raw.get("cancelled", False),
            cancelled_by=raw.get("cancelled_by"),
            disconnected=raw.get("disconnected", False),
            completed_after_disconnect=raw.get("completed_after_disconnect", False),
            fault=raw.get("fault"),
            received_at_ms=raw["received_at_ms"],
            completed_at_ms=raw.get("completed_at_ms"),
        )

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None

    def json_body(self) -> dict[str, Any]:
        return json.loads(self.body)


def _build_binary() -> Path:
    if MCP_MOCK_BIN.exists():
        return MCP_MOCK_BIN
    if os.environ.get("E2E_MCP_MOCK_BIN"):
        raise RuntimeError(f"E2E_MCP_MOCK_BIN does not exist: {MCP_MOCK_BIN}")
    result = subprocess.run(
        ["cargo", "build", "--release", "--bin", "mock-mcp"],
        cwd=ROOT / "bench/mock-mcp",
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert MCP_MOCK_BIN.exists(), f"cargo build did not produce {MCP_MOCK_BIN}"
    return MCP_MOCK_BIN


class MCPMock:
    """One mock process; every instance lives at /s/<name>/mcp on this port."""

    def __init__(self, *, extra_args: Iterable[str] = ()) -> None:
        self.bin = _build_binary()
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.dir = Path(
            tempfile.mkdtemp(
                prefix="lumewarp-mock-mcp-", dir=os.environ.get("E2E_RUN_DIR")
            )
        )
        self.log = self.dir / "mock-mcp.log"
        self.extra_args = list(extra_args)
        self.process: subprocess.Popen | None = None
        self.client = httpx.Client(timeout=30, base_url=self.base_url)

    @classmethod
    def start(cls, *, extra_args: Iterable[str] = ()) -> Self:
        mock = cls(extra_args=extra_args)
        mock._start()
        return mock

    def _start(self) -> None:
        argv = [str(self.bin), "--listen", f"127.0.0.1:{self.port}", *self.extra_args]
        with open(self.log, "ab") as out:
            self.process = subprocess.Popen(
                argv,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        if run_dir := os.environ.get("E2E_RUN_DIR"):
            with open(Path(run_dir) / "pids", "a") as pids:
                pids.write(f"{self.process.pid}\n")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"mock-mcp exited:\n{self.log_text()}")
            try:
                if self.client.get("/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"mock-mcp did not become ready:\n{self.log_text()}")

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        self.stop()
        # Keep the log when the test failed; the runner's E2E_RUN_DIR is removed by its script.
        if exc_type is None and not os.environ.get("E2E_RUN_DIR"):
            shutil.rmtree(self.dir, ignore_errors=True)

    def log_text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    # URLs -------------------------------------------------------------------

    def url(self, name: str) -> str:
        return f"{self.base_url}/s/{name}/mcp"

    def path(self, name: str) -> str:
        return f"/s/{name}/mcp"

    # Admin API --------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise AssertionError(
                f"{method} {path} -> {response.status_code}: {response.text}"
            )
        if response.content:
            return response.json()
        return None

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def create_server(self, server: Server) -> ServerConfig:
        raw = self._request("POST", "/admin/servers", json=server.to_json())
        return ServerConfig.from_json(raw)

    def get_server(self, name: str) -> ServerConfig:
        return ServerConfig.from_json(self._request("GET", f"/admin/servers/{name}"))

    def list_servers(self) -> list[ServerConfig]:
        raw = self._request("GET", "/admin/servers")
        return [ServerConfig.from_json(item) for item in raw["servers"]]

    def delete_server(self, name: str) -> None:
        self._request("DELETE", f"/admin/servers/{name}")

    def set_tools(self, name: str, tools: Iterable[Tool]) -> ServerConfig:
        raw = self._request(
            "PUT",
            f"/admin/servers/{name}/tools",
            json={"tools": [tool.to_json() for tool in tools]},
        )
        return ServerConfig.from_json(raw)

    def upsert_tool(self, name: str, tool: Tool) -> ServerConfig:
        raw = self._request("POST", f"/admin/servers/{name}/tools", json=tool.to_json())
        return ServerConfig.from_json(raw)

    def delete_tool(self, name: str, tool: str) -> ServerConfig:
        return ServerConfig.from_json(
            self._request("DELETE", f"/admin/servers/{name}/tools/{tool}")
        )

    def set_fault(self, name: str, fault: Fault | None) -> ServerConfig:
        raw = self._request(
            "POST",
            f"/admin/servers/{name}/fault",
            json={"fault": fault.to_json() if fault else None},
        )
        return ServerConfig.from_json(raw)

    def set_tool_fault(self, name: str, tool: str, fault: Fault | None) -> ServerConfig:
        config = self.get_server(name)
        current = config.tool(tool)
        assert current is not None, f"{name} has no tool {tool}"
        return self.upsert_tool(
            name,
            Tool(
                name=current.name,
                behaviour=current.behaviour,
                description=current.description,
                input_schema=current.input_schema,
                definition=current.definition,
                result=current.result,
                error=current.error,
                sleep_ms=current.sleep_ms,
                progress_events=current.progress_events,
                progress_interval_ms=current.progress_interval_ms,
                response=current.response,
                fault=fault,
            ),
        )

    def records(
        self,
        *,
        server: str | None = None,
        http_method: str | None = None,
        method: str | None = None,
        tool: str | None = None,
        after: int | None = None,
        limit: int | None = None,
    ) -> list[RequestRecord]:
        params: dict[str, Any] = {}
        for name, value in (
            ("server", server),
            ("http_method", http_method),
            ("method", method),
            ("tool", tool),
            ("after", after),
            ("limit", limit),
        ):
            if value is not None:
                params[name] = value
        raw = self._request("GET", "/admin/records", params=params)
        return [RequestRecord.from_json(item) for item in raw["records"]]

    def clear_records(self, *, server: str | None = None) -> int:
        params = {"server": server} if server else {}
        raw = self._request("DELETE", "/admin/records", params=params)
        return raw["cleared"]

    def set_recording(self, enabled: bool) -> bool:
        raw = self._request("PUT", "/admin/recording", json={"enabled": enabled})
        return raw["enabled"]

    def recording(self) -> bool:
        return self._request("GET", "/admin/recording")["enabled"]

    def wait_for_record(
        self,
        predicate: Callable[[RequestRecord], bool],
        *,
        timeout: float = 10,
        **filters: Any,
    ) -> RequestRecord:
        deadline = time.monotonic() + timeout
        while True:
            for record in self.records(**filters):
                if predicate(record):
                    return record
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"no matching record after {timeout}s; records={self.records(**filters)}"
                )
            time.sleep(0.02)
