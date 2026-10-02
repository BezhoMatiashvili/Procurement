#!/usr/bin/env python3
"""Download the National Bank of Georgia's official exchange rates into a file the server imports.

Run this on any computer with internet access - never on the server, which stays offline
(docs/SECURITY.md). It asks the NBG's public API for every calendar day of a range, one request a
day, and writes down what the National Bank answered for each day:

    python3 scripts/fetch_nbg_rates.py --from 2026-01-01 --to 2026-09-30 --out nbg-2026.json

Copy the file to the server and import it there:

    docker compose exec app python -m procurement_agent.cli load-fx-rates /path/to/nbg-2026.json

Only dates and currency codes leave this computer: a request names a day, never a document. The
script needs nothing but Python 3: it does not import the application, so it runs where the
application is not installed (like ``prefetch_models.sh``, it is the one place that goes online).

Each day is asked for on its own, and the answer is kept only if the rate it gives is in force on
that day - set on it or shortly before (a weekend or a holiday carries the last business day's
rate). A day the NBG has not published yet is left out and listed, so a file never holds a rate
borrowed from another day. Redirects are refused, the host is fixed, TLS is verified, an answer
is capped in size and checked field by field, and the file is written whole or not at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit

#: The NBG's public rates endpoint. A request is ``?date=YYYY-MM-DD`` (and ``currencies=USD`` …).
ENDPOINT = "https://nbg.gov.ge/gw/api/ct/monetarypolicy/currencies/en/json/"
HOST = "nbg.gov.ge"
#: The largest answer read: all 40-odd currencies of one day are a few kilobytes.
MAX_ANSWER = 1024 * 1024
#: The longest range one run fetches: thirty years of days.
MAX_DAYS = 31 * 366
#: How old a day's rate may be when it is in force: the last business day before a long holiday.
MAX_CARRY_DAYS = 10
QUANTITIES = frozenset({1, 10, 100, 1000, 10000})
_CODE = re.compile(r"[A-Z]{3}")
_RATE = re.compile(r"\d{1,9}(?:\.\d{1,6})?")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is refused: the answer must come from the host asked."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))


def day_url(day: date, currencies: list[str]) -> str:
    query = [("date", day.isoformat())] + [("currencies", c) for c in currencies]
    url = f"{ENDPOINT}?{urlencode(query)}"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HOST:  # pragma: no cover - fixed above
        msg = f"refusing to ask {parts.hostname}"
        raise ValueError(msg)
    return url


def _get(opener: urllib.request.OpenerDirector, url: str, timeout: float) -> bytes:
    with opener.open(url, timeout=timeout) as resp:  # nosec B310 - https, fixed host (day_url)
        if resp.status != 200:
            msg = f"HTTP {resp.status}"
            raise urllib.error.URLError(msg)
        body: bytes = resp.read(MAX_ANSWER + 1)
    if len(body) > MAX_ANSWER:
        msg = "answer too large"
        raise ValueError(msg)
    return body


def _rate_text(entry: dict[str, object]) -> str:
    """The rate as the NBG wrote it: its formatted text when given, else the number's text."""
    formatted = entry.get("rateFormated")
    if isinstance(formatted, str) and formatted.strip():
        return formatted.strip()
    number = entry.get("rate")
    if isinstance(number, bool) or not isinstance(number, int | float):
        msg = "no rate"
        raise ValueError(msg)
    return repr(number)


def rates_of_day(answer: bytes, day: date) -> list[dict[str, object]]:
    """The rates in force on ``day`` from the NBG's answer for it; [] when it has none yet.

    Raises ``ValueError`` for an answer that is not the NBG's shape, so a changed API stops the
    run instead of filling a file with guesses.
    """
    doc = json.loads(answer.decode("utf-8"))
    if not isinstance(doc, list) or not doc or not isinstance(doc[0], dict):
        msg = "unexpected answer (not a list of days)"
        raise ValueError(msg)
    currencies = doc[0].get("currencies")
    if not isinstance(currencies, list):
        msg = "unexpected answer (no currencies)"
        raise ValueError(msg)
    out: list[dict[str, object]] = []
    for entry in currencies:
        if not isinstance(entry, dict):
            msg = "unexpected answer (a currency is not an object)"
            raise ValueError(msg)
        code = entry.get("code")
        quantity = entry.get("quantity")
        valid = entry.get("validFromDate")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            msg = f"unexpected currency code {code!r}"
            raise ValueError(msg)
        if isinstance(quantity, bool) or quantity not in QUANTITIES:
            msg = f"unexpected quantity {quantity!r} for {code}"
            raise ValueError(msg)
        if not isinstance(valid, str):
            msg = f"no validFromDate for {code}"
            raise ValueError(msg)
        valid_from = date.fromisoformat(valid[:10])
        if not day - timedelta(days=MAX_CARRY_DAYS) <= valid_from <= day:
            continue  # not in force on this day (not published yet, or a stale answer)
        text = _rate_text(entry)
        try:
            rate = Decimal(text)
        except InvalidOperation:
            msg = f"unexpected rate {text!r} for {code}"
            raise ValueError(msg) from None
        if not _RATE.fullmatch(text) or not rate > 0:
            msg = f"unexpected rate {text!r} for {code}"
            raise ValueError(msg)
        out.append({"date": day.isoformat(), "currency": code, "quantity": quantity, "rate": text})
    return out


def fetch(
    first: date,
    last: date,
    currencies: list[str],
    *,
    delay: float = 0.25,
    timeout: float = 20.0,
    retries: int = 3,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[list[dict[str, object]], list[date]]:
    """Every rate in force on every day of ``first`` … ``last``, and the days that had none."""
    opener = opener or _opener()
    rates: list[dict[str, object]] = []
    empty: list[date] = []
    day = first
    while day <= last:
        url = day_url(day, currencies)
        found: list[dict[str, object]] = []
        for attempt in range(retries):
            try:
                found = rates_of_day(_get(opener, url, timeout), day)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == retries - 1:
                    msg = f"{day.isoformat()}: {e}"
                    raise RuntimeError(msg) from e
                time.sleep(1.0 + 2.0 * attempt)
        if found:
            rates.extend(found)
        else:
            empty.append(day)
        day += timedelta(days=1)
        if day <= last and delay > 0:
            time.sleep(delay)
    return rates, empty


def write(path: Path, rates: list[dict[str, object]], first: date, last: date) -> str:
    """Write the file whole (a temporary file moved into place) and return its SHA-256."""
    doc = {
        "source": "NBG",
        "endpoint": ENDPOINT,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "from": first.isoformat(),
        "to": last.isoformat(),
        "rates": sorted(rates, key=lambda r: (str(r["date"]), str(r["currency"]))),
    }
    data = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha256(data).hexdigest()


def _day_arg(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        msg = f"{text!r} is not an ISO date such as 2026-09-10"
        raise argparse.ArgumentTypeError(msg) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--from", dest="first", type=_day_arg, required=True)
    parser.add_argument("--to", dest="last", type=_day_arg, default=None, help="default: today")
    parser.add_argument("--currencies", default="", help="e.g. USD,EUR (default: every one)")
    parser.add_argument("--out", type=Path, required=True, help="the JSON file to write")
    parser.add_argument("--delay", type=float, default=0.25, help="seconds between requests")
    args = parser.parse_args(argv)
    last = args.last or datetime.now(UTC).date()
    if args.first > last:
        parser.error("--from is after --to")
    if (last - args.first).days + 1 > MAX_DAYS:
        parser.error(f"at most {MAX_DAYS} days at a time")
    currencies = [c.strip().upper() for c in args.currencies.split(",") if c.strip()]
    if any(not _CODE.fullmatch(c) for c in currencies):
        parser.error("--currencies takes three-letter codes such as USD,EUR")
    rates, empty = fetch(args.first, last, currencies, delay=max(0.0, args.delay))
    digest = write(args.out, rates, args.first, last)
    days = (last - args.first).days + 1
    print(f"{len(rates)} rates for {days - len(empty)} of {days} days written to {args.out}")
    print(f"sha256 {digest}")
    if empty:
        print("no rate in force yet on: " + ", ".join(d.isoformat() for d in empty[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
