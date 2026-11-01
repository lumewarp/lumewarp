import importlib
import io
from datetime import UTC, datetime, timedelta, timezone

import pytest

from lumewarp_control_plane.ids import uuid7
from lumewarp_control_plane.settings import Settings, split_bind
from lumewarp_control_plane.windows import month_window


def test_generated_protos_import() -> None:
    from lumewarp_control_plane._proto.lumewarp.config.v1 import (
        config_service_pb2,
        config_service_pb2_grpc,
        snapshot_pb2,
    )
    from lumewarp_control_plane._proto.lumewarp.usage.v1 import usage_pb2, usage_pb2_grpc

    snap = snapshot_pb2.Snapshot(version=3, scope="default")
    assert snapshot_pb2.Snapshot.FromString(snap.SerializeToString()) == snap
    assert config_service_pb2.Delta(base_version=1, version=2).version == 2
    assert usage_pb2.UsageEvent(request_id="x").request_id == "x"
    assert hasattr(config_service_pb2_grpc, "ConfigServiceServicer")
    assert hasattr(usage_pb2_grpc, "UsageServiceServicer")


@pytest.mark.parametrize(
    "module", ["api", "compiler", "grpc", "usage", "notifier", "outbox", "audit", "authz"]
)
def test_skeleton_modules_import(module: str) -> None:
    importlib.import_module(f"lumewarp_control_plane.{module}")


def test_uuid7_is_time_ordered() -> None:
    ids = [uuid7() for _ in range(5)]
    assert all(u.version == 7 and u.variant == "specified in RFC 4122" for u in ids)
    assert ids[0].int >> 80 <= ids[-1].int >> 80


def test_month_window() -> None:
    start, end = month_window(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    # Attributed by UTC, not local time.
    local = datetime(2026, 11, 1, 1, 0, tzinfo=timezone(timedelta(hours=8)))
    assert month_window(local)[0] == datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        month_window(datetime(2026, 1, 1))


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUMEWARP_HTTP_BIND", "0.0.0.0:9999")
    monkeypatch.setenv("LUMEWARP_BREAK_GLASS_PASSWORD", "s3cret")
    s = Settings()
    assert s.http_host_port == ("0.0.0.0", 9999)
    assert s.break_glass_password is not None and "s3cret" not in repr(s)
    assert s.grpc_bind == "127.0.0.1:50051"


def test_split_bind() -> None:
    assert split_bind("[::1]:8091") == ("::1", 8091)
    with pytest.raises(ValueError):
        split_bind("8091")


def test_hash_password_cli_reads_stdin_and_prints_argon2id(monkeypatch, capsys) -> None:
    from lumewarp_control_plane.__main__ import main
    from lumewarp_control_plane.auth.break_glass import PASSWORD_HASHER

    monkeypatch.setattr("sys.stdin", io.StringIO("cli-test-password\n"))
    main(["hash-password"])
    encoded = capsys.readouterr().out.strip()
    assert encoded.startswith("$argon2id$")
    assert PASSWORD_HASHER.verify(encoded, "cli-test-password")
