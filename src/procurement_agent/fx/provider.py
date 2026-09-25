"""Official exchange rates, for the invoice of an act written in a foreign currency (rule 9).

Such an act is invoiced in lari at the National Bank of Georgia's official rate: an advance at the
rate of the day it was paid, the rest at the rate of the act's date. The NBG publishes its rates
online and this system is air-gapped, so where a rate comes from is pluggable: a connector - an
offline table the operator loads, an internal mirror - implements ``RateProvider``, adds its name
to ``Settings.fx_rate_source`` and its opener to ``_SOURCES``, and nothing else changes.

None is connected yet. ``NullRateProvider`` has no rate for any day, and the invoice check then
writes out the formula with the days whose rates to look up - never a total at a guessed rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from procurement_agent.config import Settings

__all__ = ["NullRateProvider", "RateProvider", "RateQuote", "open_rate_provider"]


@dataclass(frozen=True, slots=True)
class RateQuote:
    """One official rate: what one unit of ``currency`` was worth in lari on ``valid_on``."""

    currency: str
    #: lari per ONE unit. The NBG quotes a few currencies per 10 or 100 units; a connector divides
    #: those down, so a rate here always multiplies an amount as it stands.
    rate: Decimal
    #: the day the rate applies to
    valid_on: date
    #: where it came from, as a message names it: "NBG"
    source: str


class RateProvider(Protocol):
    """A source of official rates.

    ``rate`` is None when the source has no rate for that day. The caller then says it could
    not calculate rather than borrow another day's rate - which day's rate applies on a weekend
    or a holiday is the connector's to know, not the caller's to guess.
    """

    def rate(self, currency: str, on: date) -> RateQuote | None: ...

    def close(self) -> None: ...


class NullRateProvider:
    """No rate source connected (``PA_FX_RATE_SOURCE=none``): no day has a rate."""

    def rate(self, currency: str, on: date) -> RateQuote | None:
        return None

    def close(self) -> None:
        return None


#: The rate sources, by their ``Settings.fx_rate_source`` name, each with its opener.
_SOURCES: dict[str, Callable[[Settings], RateProvider]] = {
    "none": lambda _settings: NullRateProvider(),
}


def open_rate_provider(settings: Settings) -> RateProvider:
    """The configured rate source, open. The caller closes it, as it closes the registry
    snapshot (``registry.snapshot.open_snapshot``)."""
    return _SOURCES[settings.fx_rate_source](settings)
