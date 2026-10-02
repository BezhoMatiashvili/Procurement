"""Official exchange rates, for the invoice of an act written in a foreign currency (rule 9).

Such an act is invoiced in lari at the National Bank of Georgia's official rate: an advance at the
rate of the day it was paid, the rest at the rate of the act's date. The NBG publishes its rates
online and this system is air-gapped, so where a rate comes from is pluggable: a source implements
``RateProvider``, adds its name to ``Settings.fx_rate_source`` and its opener to ``_SOURCES``, and
nothing else changes.

Two exist. ``"table"`` (the default) reads the NBG rates an operator imported from a file fetched on
a connected machine (``fx/rates_table.py``, ``procurement-agent load-fx-rates``); ``"none"`` turns
the lookup off. Over either, ``EnteredRateProvider`` puts the rates a user typed on the form for one
check. A day neither has stays without a rate, and the invoice check then writes out the formula
with the days whose rates to import or type in - never a total at a guessed rate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from procurement_agent.config import Settings

__all__ = [
    "ENTERED",
    "EnteredRateProvider",
    "NullRateProvider",
    "RateProvider",
    "RateQuote",
    "open_rate_provider",
]

#: ``RateQuote.source`` of a rate the user typed in for one check.
ENTERED = "entered"


@dataclass(frozen=True, slots=True)
class RateQuote:
    """One official rate: what one unit of ``currency`` was worth in lari on ``valid_on``."""

    currency: str
    #: lari per ONE unit. The NBG quotes a few currencies per 10 or 100 units; a connector divides
    #: those down, so a rate here always multiplies an amount as it stands.
    rate: Decimal
    #: the day the rate applies to
    valid_on: date
    #: where it came from: "NBG" for an imported official rate, ``ENTERED`` for one a user typed
    source: str


class RateProvider(Protocol):
    """A source of official rates.

    ``rate`` is None when the source has no rate for that day. The caller then says it could
    not calculate rather than borrow another day's rate - which day's rate applies on a weekend
    or a holiday is the source's to know, not the caller's to guess. ``coverage`` is the first
    and last day the source holds a currency for, so that a missing day can be explained.
    """

    def rate(self, currency: str, on: date) -> RateQuote | None: ...

    def coverage(self, currency: str) -> tuple[date, date] | None: ...

    def close(self) -> None: ...


class NullRateProvider:
    """No rate source (``PA_FX_RATE_SOURCE=none``): no day has a rate."""

    def rate(self, currency: str, on: date) -> RateQuote | None:
        return None

    def coverage(self, currency: str) -> tuple[date, date] | None:
        return None

    def close(self) -> None:
        return None


class EnteredRateProvider:
    """The rates a user typed in for one check, over the configured source.

    ``entered`` maps (currency, day) to lari per ONE unit. A typed rate wins over the source's for
    its day - the user may be correcting it - and is marked ``ENTERED`` so the report says whose
    figure the invoice uses. Closing it closes the source beneath.
    """

    def __init__(self, entered: Mapping[tuple[str, date], Decimal], under: RateProvider) -> None:
        self._entered = {(c.upper(), d): r for (c, d), r in entered.items()}
        self._under = under

    def rate(self, currency: str, on: date) -> RateQuote | None:
        typed = self._entered.get((currency.upper(), on))
        if typed is not None:
            return RateQuote(currency=currency.upper(), rate=typed, valid_on=on, source=ENTERED)
        return self._under.rate(currency, on)

    def coverage(self, currency: str) -> tuple[date, date] | None:
        return self._under.coverage(currency)

    def close(self) -> None:
        self._under.close()


def _open_table(settings: Settings) -> RateProvider:
    # imported here: the table module builds ``RateQuote``s, so it imports this one
    from procurement_agent.fx.rates_table import TableRateProvider

    return TableRateProvider(settings.fx_rates_path)


#: The rate sources, by their ``Settings.fx_rate_source`` name, each with its opener.
_SOURCES: dict[str, Callable[[Settings], RateProvider]] = {
    "none": lambda _settings: NullRateProvider(),
    "table": _open_table,
}


def open_rate_provider(
    settings: Settings, entered: Mapping[tuple[str, date], Decimal] | None = None
) -> RateProvider:
    """The configured rate source, open, with the rates a user typed in on top. The caller
    closes it, as it closes the registry snapshot (``registry.snapshot.open_snapshot``)."""
    source = _SOURCES[settings.fx_rate_source](settings)
    return EnteredRateProvider(entered, source) if entered else source
