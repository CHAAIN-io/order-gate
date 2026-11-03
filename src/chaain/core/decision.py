"""What the gate says about a sale. Rules produce it; the run acts on it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    AUTO = "AUTO"  # send now
    HOLD = "HOLD"  # a person decides
    SKIP = "SKIP"  # not for the warehouse


@dataclass(frozen=True)
class Decision:
    route: Route
    reasons: tuple[str, ...]
    final: bool = False  # a SKIP that must never be reconsidered (e.g. #NOVISION)

    def __post_init__(self) -> None:
        if self.route is not Route.AUTO and not self.reasons:
            raise ValueError(f"a {self.route.value} needs at least one reason")
        if self.final and self.route is not Route.SKIP:
            raise ValueError("only a SKIP can be final")

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) or "passed every check"
