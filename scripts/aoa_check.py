"""Prototype of the phase-4 check: is an Act of Acceptance consistent with its contract?

Works purely on extraction results produced by the running app (rules-only is enough for the
structured fields). Usage:

    uv run python scripts/aoa_check.py --base http://127.0.0.1:8001 --key pa_... \\
        --contract <contract job id> --aoa <name>=<aoa job id> [--aoa ...]
"""

from __future__ import annotations

import argparse
import calendar
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

MONTHLY = "monthly"


def facts_by_type(result: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for f in result["facts"]:
        out[f["type"]].append(f)
    return out


def values(fb: dict[str, list[dict]], t: str) -> set[str]:
    return {str(f.get("normalized") or f["value"]) for f in fb.get(t, [])}


def dates(fb: dict[str, list[dict]], *types: str) -> list[date]:
    out = []
    for t in types:
        for f in fb.get(t, []):
            n = f.get("normalized")
            if isinstance(n, str) and len(n) == 10:
                out.append(date.fromisoformat(n))
    return sorted(set(out))


def add_months(start: date, months: int) -> date:
    """``start`` moved by whole months, clamped to the last day of the target month.

    ``date(year, month, start.day)`` raised ValueError for any term starting on the 29th-31st
    whose end month is shorter - a contract signed on 31 January could not be checked at all.
    """
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def load_result(
    ref: str,
    *,
    base: str | None,
    key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """A job result from a local JSON file or from the running app's API.

    The client is closed after the request, and an HTTP error is raised as such: the result
    of a 404 used to be parsed as though it were a job and failed later as a KeyError.
    """
    if ref.endswith(".json"):
        return json.loads(Path(ref).read_text(encoding="utf-8"))
    if not base or not key:
        raise SystemExit("--base and --key are required to fetch a job result from the API")
    with httpx.Client(
        base_url=base, headers={"X-API-Key": key}, timeout=30, transport=transport
    ) as client:
        response = client.get(f"/api/v1/jobs/{ref}/result")
        response.raise_for_status()
        return response.json()


def check(contract: dict, aoa: dict) -> list[str]:
    c, a = facts_by_type(contract), facts_by_type(aoa)
    issues: list[str] = []

    # 1. parties: identification codes must match the contract's set
    def id_codes(fb: dict[str, list[dict]]) -> set[str]:
        # the LLM sometimes labels a cadastral code as an ID code; real ones are 9 or 11 digits
        return {v for v in values(fb, "party_id_code") if v.isdigit() and len(v) in (9, 11)}

    c_ids, a_ids = id_codes(c), id_codes(a)
    if not a_ids:
        issues.append("no identification codes found in the act")
    elif c_ids and a_ids != c_ids:
        issues.append(
            f"identification codes differ: act {sorted(a_ids)} vs contract {sorted(c_ids)}"
        )

    # 2. cadastral code / project
    c_cad, a_cad = values(c, "cadastral_code"), values(a, "cadastral_code")
    if not a_cad:
        issues.append("cadastral code missing in the act")
    elif c_cad and a_cad != c_cad:
        issues.append(f"cadastral code differs: act {sorted(a_cad)} vs contract {sorted(c_cad)}")

    # 3. bank requisites
    for unit, label in (("IBAN", "IBAN"), ("SWIFT", "bank code")):
        c_b = {f["value"] for f in c.get("bank_account", []) if f.get("unit") == unit}
        a_b = {f["value"] for f in a.get("bank_account", []) if f.get("unit") == unit}
        if c_b and a_b and a_b != c_b:
            issues.append(f"{label} differs: act {sorted(a_b)} vs contract {sorted(c_b)}")
        if not a_b and unit == "IBAN":
            issues.append("IBAN missing in the act")

    # 4. amount: act fee must equal a contract amount (monthly fee here)
    c_amt = {
        float(f["normalized"])
        for t in ("price", "total_amount", "amount")
        for f in c.get(t, [])
        if f.get("normalized") is not None
    }
    a_amt = {
        float(f["normalized"])
        for t in ("price", "total_amount", "amount")
        for f in a.get(t, [])
        if f.get("normalized") is not None
    }
    if not a_amt:
        issues.append("no fee amount found in the act")
    elif c_amt and not (a_amt & c_amt):
        issues.append(f"fee {sorted(a_amt)} does not match any contract amount {sorted(c_amt)}")

    # 5. execution period inside the contract term; act date = last day of the period
    period = dates(a, "execution_period")
    term = dates(c, "contract_date", "execution_period", "date", "validity_period")
    if len(term) == 1:  # only the start date survived extraction: use the stated duration
        months = [
            int(f["normalized"][1:-1])
            for t in ("duration", "execution_period", "validity_period")
            for f in c.get(t, [])
            if isinstance(f.get("normalized"), str) and f["normalized"].endswith("M")
        ]
        if months:
            term.append(add_months(term[0], max(months)))
    act_dates = [d for d in dates(a, "contract_date") if d not in term]  # act's own date(s)
    if len(period) < 2:
        issues.append("execution period (start and end) not found in the act")
    else:
        start, end = period[0], period[-1]
        if term and (start < min(term) or end > max(term)):
            issues.append(
                f"execution period {start}..{end} is outside the contract term "
                f"{min(term)}..{max(term)}"
            )
        last_day = date(end.year, end.month, calendar.monthrange(end.year, end.month)[1])
        if not act_dates:
            issues.append("act date not found")
        elif all(d != end for d in act_dates):
            issues.append(
                f"act dated {[str(d) for d in act_dates]} but the service period ends {end} "
                f"(act should be dated the last day, {last_day})"
            )

    # 6. party names / signatories (available when the LLM pass ran on both documents)
    def norm_names(fb: dict[str, list[dict]], t: str) -> set[str]:
        out = set()
        for f in fb.get(t, []):
            n = f["value"].lower()
            for ch in "„“”\"'«»,.":
                n = n.replace(ch, " ")  # „შპს“ნიმუში“ vs „შპს „ნიმუში“: quotes act as spaces
            out.add(" ".join(n.split()))
        return out

    for t, label in (("party_name", "party name"), ("party_director", "signatory")):
        c_n, a_n = norm_names(c, t), norm_names(a, t)
        if c_n and a_n:
            unknown = {n for n in a_n if not any(n in cn or cn in n for cn in c_n)}
            if unknown:
                issues.append(f"{label}(s) not in the contract: {sorted(unknown)}")

    # 7. conflicts flagged by the extractor itself
    for f in aoa["facts"]:
        if f["conflict"] and f["type"] not in ("contract_date",):
            issues.append(f"inconsistent {f['type']}: {f['value']}")
    return issues


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="app base URL (API mode)")
    ap.add_argument("--key", help="API key (API mode)")
    ap.add_argument("--contract", required=True, help="job id (API mode) or result .json path")
    ap.add_argument("--aoa", action="append", required=True, help="name=<job id | json path>")
    args = ap.parse_args()

    contract = load_result(args.contract, base=args.base, key=args.key)
    for spec in args.aoa:
        name, ref = spec.split("=", 1)
        aoa = load_result(ref, base=args.base, key=args.key)
        issues = check(contract, aoa)
        print(f"\n== {name}: {'OK' if not issues else f'{len(issues)} issue(s)'}")
        for i in issues:
            print("   -", i)


if __name__ == "__main__":
    main()
