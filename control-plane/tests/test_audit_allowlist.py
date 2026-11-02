"""The audit `details` allowlist covers every recorded action and drops everything else."""

import ast
from pathlib import Path

from lumewarp_control_plane import audit

SOURCE = Path(audit.__file__).parent
# Actions are string constants (or both arms of a conditional) in `audit.record(action=...)`.
SKIP_DIRS = {"migrations", "_proto"}


def recorded_actions() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record"
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg != "action":
                    continue
                found.update(
                    value.value
                    for value in ast.walk(keyword.value)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                )
    return found


def test_every_audited_action_is_classified() -> None:
    """A new action without an allowlist would silently record no details; fail here."""
    assert recorded_actions() == set(audit.DETAIL_ALLOWLIST)


def test_unknown_action_records_no_details() -> None:
    assert audit.details_of("nope.nothing", {"name": "private"}) == {}


def test_unknown_fields_are_dropped() -> None:
    assert audit.details_of(
        "key.create",
        {
            "name": "k",
            "owner_kind": "user",
            "secret": "lw-should-not-be-stored",
            "nested": {"Authorization": "Bearer x"},
            "items": [{"token": "t"}],
        },
    ) == {"name": "k", "owner_kind": "user"}


def test_nested_fields_are_filtered_too() -> None:
    assert audit.details_of(
        "upstream.update",
        {
            "name": "openai",
            "changes": {
                "base_url": "https://api.example/v1",
                "api_key_ref": "OPENAI_API_KEY",
                "secret": "sk-...",
                "health_check": {"path": "/health", "headers": {"authorization": "Bearer x"}},
            },
            "unclassified": "drop me",
        },
    ) == {
        "name": "openai",
        "changes": {
            "base_url": "https://api.example/v1",
            "api_key_ref": "OPENAI_API_KEY",
            "health_check": {"path": "/health"},
        },
    }


def test_strategy_params_are_never_audited() -> None:
    """Router strategy params are free-form JSON, so only classified router fields survive."""
    assert audit.details_of(
        "router.update",
        {
            "name": "demo",
            "changes": {
                "params": {"api_key": "secret"},
                "members": [{"model_id": "m1", "weight": 2, "params": {"secret": "x"}}],
                "traffic_split": [
                    {
                        "name": "canary",
                        "weight": 10,
                        "members": [{"model_id": "m1", "weight": 1, "params": {"x": 1}}],
                    }
                ],
            },
        },
    ) == {
        "name": "demo",
        "changes": {
            "members": [{"model_id": "m1", "weight": 2}],
            "traffic_split": [
                {"name": "canary", "weight": 10, "members": [{"model_id": "m1", "weight": 1}]}
            ],
        },
    }


def test_mapping_under_a_scalar_field_is_dropped() -> None:
    assert audit.details_of(
        "session.break_glass.request",
        {"method": "POST", "path": {"nested": "not a path"}, "status": 403},
    ) == {"method": "POST", "status": 403}


def test_explicit_null_removal_is_recorded() -> None:
    assert audit.details_of("router.update", {"changes": {"retry": None}}) == {
        "changes": {"retry": None}
    }
