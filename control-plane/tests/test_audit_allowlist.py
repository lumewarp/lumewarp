"""The audit `details` allowlist covers every recorded action and drops everything else."""

import ast
from pathlib import Path

import pytest

from lumewarp_control_plane import audit

SOURCE = Path(audit.__file__).parent
SKIP_DIRS = {"migrations", "_proto"}


class UnresolvedAction(ValueError):
    """An `action=` expression the guard cannot prove to be string constants."""


def action_names(expression: ast.expr) -> set[str]:
    """Resolve the accepted forms: a string literal, or a conditional of them."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        if not expression.value:
            raise UnresolvedAction("empty action")
        return {expression.value}
    if isinstance(expression, ast.IfExp):
        return action_names(expression.body) | action_names(expression.orelse)
    raise UnresolvedAction(ast.unparse(expression))


def actions_in(source: str, *, where: str = "<test>") -> set[str]:
    """Every action passed to a `record(...)` call; unresolvable ones raise, never skip."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `audit.record(...)` or a directly imported `record(...)`.
        if not (
            (isinstance(func, ast.Attribute) and func.attr == "record")
            or (isinstance(func, ast.Name) and func.id == "record")
        ):
            continue
        keyword = next((item for item in node.keywords if item.arg == "action"), None)
        if keyword is None:
            raise UnresolvedAction(f"{where}: record() without an action= keyword")
        try:
            found |= action_names(keyword.value)
        except UnresolvedAction as exc:
            raise UnresolvedAction(f"{where}: {exc}") from exc
    return found


def recorded_actions() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        found |= actions_in(path.read_text(), where=str(path))
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('audit.record(session, action="key.create")', {"key.create"}),
        (
            'audit.record(session, action="member.add" if created else "member.update")',
            {"member.add", "member.update"},
        ),
        (
            'audit.record(session, action=("a.b" if x else ("c.d" if y else "e.f")))',
            {"a.b", "c.d", "e.f"},
        ),
    ],
)
def test_literal_and_conditional_actions_are_accepted(source, expected) -> None:
    assert actions_in(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        'action = "key.denied"\naudit.record(session, action=action)',  # Name
        'audit.record(session, action="prefix." + suffix)',  # dynamic expression
        "audit.record(session, action=ACTIONS[0])",
        "audit.record(session, action=make_action())",
        'audit.record(session, action=f"{kind}.denied")',
        "audit.record(session, action=1)",
        "audit.record(session)",
        "audit.record(session, **kwargs)",
        'record(session, action="key.create" if x else make_action())',  # bare import
    ],
)
def test_unresolvable_actions_fail_the_guard(source) -> None:
    with pytest.raises(UnresolvedAction):
        actions_in(source)


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
                "health_url": "http://h/health",
                "health_check": {
                    "interval_ms": 5000,
                    "headers": {"authorization": "Bearer x"},
                },
            },
            "unclassified": "drop me",
        },
    ) == {
        "name": "openai",
        "changes": {
            "base_url": "https://api.example/v1",
            "api_key_ref": "OPENAI_API_KEY",
            "health_url": "http://h/health",
            "health_check": {"interval_ms": 5000},
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
