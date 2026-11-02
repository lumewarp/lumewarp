"""The Python router the worked example starts from.

`CheapestFirstRouter.choose` receives the deployments that are healthy right
now, for the model being called, in configuration order. Returning `None` means
"nothing acceptable"; the caller takes its fallback path.

Prices are nano-USD per million input tokens, the unit Lumewarp's model catalog
uses. A deployment without an entry in `prices` is unpriced.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Deployment:
    """What the router knows about one deployment of the called model."""

    name: str
    in_flight: int = 0


class CheapestFirstRouter:
    """Pick the lowest input price among the healthy deployments.

    * Ties go to fewer requests in flight, then to configuration order.
    * An unpriced deployment is eligible only when nothing eligible is priced:
      a price that is not known cannot be compared with one that is.
    * `max_input_price` (nano-USD per Mtok) refuses anything above the cap and
      refuses unpriced deployments too; with nothing left, the caller's
      fallback runs.

    The gateway filters health and traffic splits before calling `choose`, so
    this class never sees a model the router already excluded.
    """

    def __init__(
        self,
        prices: dict[str, int],
        max_input_price: int | None = None,
    ) -> None:
        self._prices = dict(prices)
        self._max_input_price = max_input_price

    def choose(self, deployments: list[Deployment]) -> Deployment | None:
        best: Deployment | None = None
        best_key: tuple[bool, int, int, int] | None = None
        for position, deployment in enumerate(deployments):
            price = self._prices.get(deployment.name)
            if self._max_input_price is not None and (
                price is None or price > self._max_input_price
            ):
                continue
            key = (price is None, price or 0, deployment.in_flight, position)
            if best_key is None or key < best_key:
                best, best_key = deployment, key
        return best


if __name__ == "__main__":
    router = CheapestFirstRouter({"gpt-4o": 2_500_000_000, "gpt-4o-mini": 150_000_000})
    print(
        router.choose(
            [Deployment("gpt-4o"), Deployment("gpt-4o-mini", in_flight=4)]
        )
    )
