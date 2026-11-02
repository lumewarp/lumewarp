"""Regenerate the crate's test table by running the Python policy itself.

    python3 .claude/skills/port-router-to-rust/example/gen_pick_table.py [OUT]

Run it from the repository root: OUT defaults to the worked example's
`gateway/strategies/cheapest-first/testdata/pick-table.json`, resolved against
the current directory. Pass a path when the skill is used from elsewhere.

The Rust test `pick_matches_the_python_policy` reads the file and asserts that
the crate picks the same deployment for every case. Never edit the JSON by
hand: add a case below and run this script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cheapest_first import CheapestFirstRouter, Deployment

DEFAULT_OUT = Path("gateway/strategies/cheapest-first/testdata/pick-table.json")


def case(
    name: str,
    members: list[tuple[str, int | None]],
    *,
    params: dict[str, int] | None = None,
    eligible: list[bool] | None = None,
    in_flight: list[int] | None = None,
) -> dict:
    """Run the Python policy once. `members` is `(model, price or None)`."""
    params = params or {}
    eligible = eligible if eligible is not None else [True] * len(members)
    in_flight = in_flight if in_flight is not None else [0] * len(members)
    assert len(eligible) == len(in_flight) == len(members)
    prices = {model: price for model, price in members if price is not None}
    router = CheapestFirstRouter(
        prices, params.get("max_input_price_nano_usd_per_mtok")
    )
    deployments = [
        Deployment(model, load)
        for (model, _), ok, load in zip(members, eligible, in_flight, strict=True)
        if ok
    ]
    chosen = router.choose(deployments)
    return {
        "name": name,
        "params": params,
        "members": [
            {
                "model": model,
                "params": (
                    {} if price is None else {"input_price_nano_usd_per_mtok": price}
                ),
            }
            for model, price in members
        ],
        "eligible": eligible,
        "in_flight": in_flight,
        "pick": None if chosen is None else chosen.name,
    }


CASES = [
    case(
        "the cheapest priced member wins",
        [("a", 300), ("b", 100), ("c", 200)],
    ),
    case(
        "equal prices prefer fewer requests in flight",
        [("a", 100), ("b", 100)],
        in_flight=[3, 1],
    ),
    case(
        "equal prices and load prefer configuration order",
        [("a", 100), ("b", 100)],
    ),
    case(
        "an unpriced member comes after every priced one",
        [("priced", 500), ("unpriced", None)],
        in_flight=[4, 0],
    ),
    case(
        "with nothing priced, load then order decide among unpriced members",
        [("a", None), ("b", None)],
        in_flight=[4, 1],
    ),
    case(
        "zero is a price: free beats paid",
        [("free", 0), ("paid", 1)],
        in_flight=[9, 0],
    ),
    case(
        "a member over the cap is not eligible",
        [("a", 100), ("b", 300)],
        params={"max_input_price_nano_usd_per_mtok": 200},
    ),
    case(
        "the cap cannot be checked against an unknown price",
        [("unpriced", None), ("b", 300)],
        params={"max_input_price_nano_usd_per_mtok": 200},
    ),
    case(
        "a cap with nothing under it picks nothing",
        [("a", 100), ("b", 300)],
        params={"max_input_price_nano_usd_per_mtok": 50},
    ),
    case(
        "free is under any cap",
        [("free", 0), ("unpriced", None)],
        params={"max_input_price_nano_usd_per_mtok": 1},
    ),
    case(
        "ineligible priced members leave the priced ones that remain",
        [("a", 100), ("b", 200), ("c", 200)],
        eligible=[False, True, True],
        in_flight=[0, 3, 1],
    ),
    case(
        "an unpriced member is used when every priced member is ineligible",
        [("a", 100), ("unpriced", None)],
        eligible=[False, True],
    ),
    case(
        "nothing eligible picks nothing",
        [("a", 100)],
        eligible=[False],
    ),
]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_by": "python3 .claude/skills/port-router-to-rust/example/gen_pick_table.py",
                "cases": CASES,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"wrote {len(CASES)} cases to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
