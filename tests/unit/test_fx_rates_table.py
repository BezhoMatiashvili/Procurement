"""Rule 9's rate source: NBG rates imported from a file and looked up by exact day. Synthetic.

The two rates are the client's own example - 2.6104 for 10 September 2026 and 2.6081 for the
17th, the rates the National Bank published for those days - and so is the invoice they make,
1,000 x 2.6104 + 10,000 x 2.6081 = 28,691.40 GEL. Everything else is invented.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from procurement_agent import cli
from procurement_agent.checks.aoa import check_aoa
from procurement_agent.config import Settings, get_settings
from procurement_agent.fx import rates_table
from procurement_agent.fx.provider import (
    ENTERED,
    EnteredRateProvider,
    NullRateProvider,
    open_rate_provider,
)
from procurement_agent.fx.rates_table import (
    RatesFormatError,
    TableRateProvider,
    import_rates,
    open_rates_table,
    parse_rates,
)
from tests.unit.test_fx_invoice import (
    SMALL_ACT,
    SMALL_CONTRACT,
    USD_ACT,
    USD_CONTRACT,
    _note,
    _skip,
)

TODAY = date(2026, 10, 2)
SEP_10, SEP_17 = date(2026, 9, 10), date(2026, 9, 17)
CLIENT: list[dict[str, object]] = [
    {"date": "2026-09-10", "currency": "USD", "quantity": 1, "rate": "2.6104"},
    {"date": "2026-09-17", "currency": "USD", "quantity": 1, "rate": "2.6081"},
]


def _json(tmp_path: Path, rates: object, *, source: str = "NBG", name: str = "r.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"source": source, "rates": rates}), encoding="utf-8")
    return path


def _table(tmp_path: Path, rates: list[dict[str, object]] | None = None) -> Path:
    db = tmp_path / "fx.db"
    import_rates(parse_rates(_json(tmp_path, rates or CLIENT), today=TODAY), db)
    return db


@pytest.fixture
def provider(tmp_path: Path) -> Iterator[TableRateProvider]:
    table = TableRateProvider(_table(tmp_path))
    yield table
    table.close()


# --- the client's examples, from an imported file --------------------------------------------


def test_the_clients_example_with_an_advance(provider: TableRateProvider) -> None:
    report = check_aoa(aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=provider)
    note = _note(report)
    assert (
        "1,000.00 USD × NBG rate of 2026-09-10 (2.6104) = 2,610.40 GEL + 10,000.00 USD "
        "× NBG rate of 2026-09-17 (2.6081) = 26,081.00 GEL; in total 28,691.40 GEL"
    ) in note.message_en
    assert "verify the invoice" in note.message_en
    assert report.summary["invoice_gel"] == "28691.40"
    assert not [s for s in report.skipped if s.rule_id == "AOA.FX_RATE"]


def test_the_clients_example_without_an_advance(provider: TableRateProvider) -> None:
    report = check_aoa(aoa_text=SMALL_ACT, contract_text=SMALL_CONTRACT, rates=provider)
    assert "1,000.00 USD × NBG rate of 2026-09-10 (2.6104) = 2,610.40 GEL" in (
        _note(report).message_en
    )
    assert report.summary["invoice_gel"] == "2610.40"  # the client's "2,610.04" is a typo


# --- lookups ----------------------------------------------------------------------------------


def test_a_currency_quoted_per_hundred_units_is_divided_to_one_unit(tmp_path: Path) -> None:
    db = _table(
        tmp_path, [{"date": "2026-09-10", "currency": "JPY", "quantity": 100, "rate": "1.8123"}]
    )
    p = TableRateProvider(db)
    quote = p.rate("JPY", SEP_10)
    assert quote is not None
    assert quote.rate == Decimal("0.018123")
    # 250,000 yen at that rate, as the invoice rounds it
    assert (Decimal(250000) * quote.rate).quantize(Decimal("0.01")) == Decimal("4530.75")
    p.close()


def test_lookup_is_exact_and_never_borrows_a_neighbouring_day(provider: TableRateProvider) -> None:
    assert provider.rate("USD", SEP_10) is not None
    assert provider.rate("USD", date(2026, 9, 11)) is None  # the 11th was never imported
    assert provider.rate("EUR", SEP_10) is None
    quote = provider.rate("usd", SEP_17)
    assert quote is not None
    assert (quote.currency, quote.rate, quote.valid_on, quote.source) == (
        "USD",
        Decimal("2.6081"),
        SEP_17,
        "NBG",
    )


def test_coverage_names_the_first_and_last_day_and_the_gaps(tmp_path: Path) -> None:
    db = _table(tmp_path)
    table = open_rates_table(db)
    assert table is not None
    with table:
        [usd] = table.coverage()
        assert (usd.currency, usd.first, usd.last, usd.days, usd.missing) == (
            "USD",
            SEP_10,
            SEP_17,
            2,
            6,
        )
    p = TableRateProvider(db)
    assert p.coverage("USD") == (SEP_10, SEP_17)
    assert p.coverage("EUR") is None
    p.close()


def test_nothing_imported_means_no_rate_and_no_coverage(tmp_path: Path) -> None:
    p = TableRateProvider(tmp_path / "missing.db")
    assert not p.loaded
    assert p.rate("USD", SEP_10) is None
    assert p.coverage("USD") is None
    p.close()


def test_a_file_that_is_not_a_rates_table_counts_as_nothing_imported(tmp_path: Path) -> None:
    junk = tmp_path / "fx.db"
    junk.write_bytes(b"not a database at all" * 100)
    assert open_rates_table(junk) is None
    assert TableRateProvider(junk).rate("USD", SEP_10) is None


# --- reading a file ---------------------------------------------------------------------------


def test_a_csv_file_reads_like_the_json_one(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text(
        "﻿rate,Currency,date,quantity\n2.6104,usd,2026-09-10,1\n18.123,JPY,2026-09-10,1000\n",
        encoding="utf-8",
    )
    rows = {(r.currency, r.valid_on): r for r in parse_rates(path, today=TODAY)}
    assert rows["USD", SEP_10].per_unit == Decimal("2.6104")
    assert rows["JPY", SEP_10].per_unit == Decimal("0.018123")


def test_a_rate_written_as_a_json_number_keeps_its_digits(tmp_path: Path) -> None:
    path = _json(
        tmp_path, [{"date": "2026-09-10", "currency": "USD", "quantity": 1, "rate": 2.6104}]
    )
    [row] = parse_rates(path, today=TODAY)
    assert row.rate == Decimal("2.6104")


def test_the_quantity_defaults_to_one_in_json(tmp_path: Path) -> None:
    [row] = parse_rates(
        _json(tmp_path, [{"date": "2026-09-10", "currency": "EUR", "rate": "3.0247"}]), today=TODAY
    )
    assert (row.quantity, row.per_unit) == (1, Decimal("3.0247"))


_GOOD = {"date": "2026-09-10", "currency": "USD", "quantity": 1, "rate": "2.6104"}


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"currency": "US"}, "three-letter ISO 4217 code"),
        ({"currency": "USD1"}, "three-letter ISO 4217 code"),
        ({"currency": 840}, "three-letter ISO 4217 code"),
        ({"currency": "GEL"}, "no exchange rate against itself"),
        ({"quantity": 7}, "the quantity must be one of"),
        ({"quantity": True}, "whole number"),
        ({"quantity": "100000000"}, "the quantity must be one of"),
        ({"rate": "0"}, "not a usable exchange rate"),
        ({"rate": "-2.61"}, "not a usable exchange rate"),
        ({"rate": "NaN"}, "not a usable exchange rate"),
        ({"rate": "Infinity"}, "not a usable exchange rate"),
        ({"rate": "1000000"}, "not a usable exchange rate"),
        ({"rate": "2.6104123"}, "more than 6 decimal places"),
        ({"rate": "two"}, "is not a number"),
        ({"rate": None}, "must be a number"),
        ({"rate": False}, "must be a number"),
        ({"rate": "9" * 30}, "must be a number"),
        ({"date": "10.09.2026"}, "not an ISO date"),
        ({"date": "2026-02-30"}, "not an ISO date"),
        ({"date": 20260910}, "must be an ISO date"),
        ({"date": "1995-09-24"}, "is outside"),
        ({"date": "2026-10-12"}, "is outside"),  # ten days after "today"
    ],
)
def test_a_bad_value_refuses_the_file_and_names_the_row(
    tmp_path: Path, change: dict[str, object], said: str
) -> None:
    bad = {**_GOOD, **change}
    with pytest.raises(RatesFormatError, match="rate 2") as caught:
        parse_rates(_json(tmp_path, [_GOOD, bad]), today=TODAY)
    assert said in str(caught.value)


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (b"[1, 2]", "expected an object"),
        (b'{"rates": {"USD": 2.6}}', "expected an object"),
        (b"{not json", "not JSON"),
        (b'{"rates": ["x"]}', "each rate must be an object"),
        (b'{"source": "<script>", "rates": []}', "the source must be a short name"),
    ],
)
def test_a_malformed_json_file_is_refused(tmp_path: Path, content: bytes, said: str) -> None:
    path = tmp_path / "r.json"
    path.write_bytes(content)
    with pytest.raises(RatesFormatError, match=said):
        parse_rates(path, today=TODAY)


def test_a_csv_without_the_columns_or_in_another_encoding_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text("day,code,value\n2026-09-10,USD,2.6104\n", encoding="utf-8")
    with pytest.raises(RatesFormatError, match="needs the columns"):
        parse_rates(path, today=TODAY)
    path.write_bytes("date,currency,quantity,rate\n".encode("utf-16"))
    with pytest.raises(RatesFormatError, match="must be UTF-8"):
        parse_rates(path, today=TODAY)


def test_another_kind_of_file_or_one_too_large_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    txt = tmp_path / "r.txt"
    txt.write_text("2.6104", encoding="utf-8")
    with pytest.raises(RatesFormatError, match=r"must be \.json or \.csv"):
        parse_rates(txt, today=TODAY)
    monkeypatch.setattr(rates_table, "MAX_FILE_BYTES", 10)
    with pytest.raises(RatesFormatError, match="larger than"):
        parse_rates(_json(tmp_path, CLIENT), today=TODAY)


def test_one_day_given_two_rates_is_refused_and_the_same_rate_twice_is_one_row(
    tmp_path: Path,
) -> None:
    twice = [*CLIENT, {**CLIENT[0], "rate": "2.6105"}]
    with pytest.raises(RatesFormatError, match="two different rates"):
        parse_rates(_json(tmp_path, twice), today=TODAY)
    # 100 yen at 1.8123 and 1 yen at 0.018123 are the same rate, written two ways
    same = [
        {"date": "2026-09-10", "currency": "JPY", "quantity": 100, "rate": "1.8123"},
        {"date": "2026-09-10", "currency": "JPY", "quantity": 1, "rate": "0.018123"},
        CLIENT[0],
        CLIENT[0],
    ]
    assert len(parse_rates(_json(tmp_path, same), today=TODAY)) == 2


# --- importing --------------------------------------------------------------------------------


def test_importing_again_counts_what_was_added_changed_and_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "fx.db"
    first = import_rates(parse_rates(_json(tmp_path, CLIENT), today=TODAY), db)
    assert (first.added, first.changed, first.unchanged) == (2, 0, 0)
    assert (first.currencies, first.first, first.last) == (("USD",), SEP_10, SEP_17)
    later = [
        CLIENT[0],
        {**CLIENT[1], "rate": "2.6082"},
        {"date": "2026-09-18", "currency": "USD", "quantity": 1, "rate": "2.6090"},
    ]
    again = import_rates(parse_rates(_json(tmp_path, later), today=TODAY), db)
    assert (again.added, again.changed, again.unchanged) == (1, 1, 1)
    p = TableRateProvider(db)
    quote = p.rate("USD", SEP_17)
    assert quote is not None
    assert quote.rate == Decimal("2.6082")  # the newer file's rate
    p.close()


def test_the_table_file_is_private(tmp_path: Path) -> None:
    db = _table(tmp_path)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


# --- rates typed in by the user ---------------------------------------------------------------


def test_a_typed_rate_wins_over_the_imported_one_and_says_so(provider: TableRateProvider) -> None:
    entered = EnteredRateProvider({("usd", SEP_10): Decimal("2.6200")}, provider)
    typed = entered.rate("USD", SEP_10)
    assert typed is not None
    assert (typed.rate, typed.source) == (Decimal("2.6200"), ENTERED)
    imported = entered.rate("USD", SEP_17)
    assert imported is not None
    assert imported.source == "NBG"
    assert entered.coverage("USD") == (SEP_10, SEP_17)


def test_a_typed_rate_is_named_in_the_invoice_note(tmp_path: Path) -> None:
    entered = EnteredRateProvider(
        {("USD", SEP_10): Decimal("2.6104"), ("USD", SEP_17): Decimal("2.6081")},
        NullRateProvider(),
    )
    note = _note(check_aoa(aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=entered))
    assert "1,000.00 USD × the rate entered for 2026-09-10 (2.6104) = 2,610.40 GEL" in (
        note.message_en
    )
    assert "in total 28,691.40 GEL" in note.message_en
    assert "შეყვანილი კურსი (2026-09-10: 2.6104)" in note.message_ka


def test_closing_the_typed_rates_closes_the_source_beneath() -> None:
    closed: list[bool] = []

    class Source(NullRateProvider):
        def close(self) -> None:
            closed.append(True)

    EnteredRateProvider({}, Source()).close()
    assert closed == [True]


# --- which source a check opens ---------------------------------------------------------------


def test_the_configured_source_is_opened_with_the_typed_rates_on_top(tmp_path: Path) -> None:
    db = _table(tmp_path)
    settings = Settings(data_dir=tmp_path / "data", fx_rates_db=db)
    assert settings.fx_rate_source == "table"
    plain = open_rate_provider(settings)
    assert isinstance(plain, TableRateProvider)
    plain.close()
    layered = open_rate_provider(settings, {("USD", SEP_10): Decimal("2.62")})
    assert isinstance(layered, EnteredRateProvider)
    layered.close()
    assert isinstance(open_rate_provider(Settings(fx_rate_source="none")), NullRateProvider)
    # the default place of the table is beside the database
    assert Settings(data_dir=tmp_path / "d").fx_rates_path == tmp_path / "d" / "fx_rates.db"


# --- what the check says when a day has no rate -----------------------------------------------


def test_with_nothing_imported_the_skip_asks_for_the_rates_of_the_days(tmp_path: Path) -> None:
    report = check_aoa(
        aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=TableRateProvider(tmp_path / "x.db")
    )
    assert _skip(report, "AOA.FX_RATE") == (
        "no NBG USD rates have been imported, so the invoice total in lari was not calculated; "
        "enter the USD rate of: 2026-09-10, 2026-09-17"
    )


def test_a_day_outside_the_imported_ones_names_what_is_held(tmp_path: Path) -> None:
    db = _table(tmp_path, [CLIENT[0]])
    report = check_aoa(aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=TableRateProvider(db))
    assert _skip(report, "AOA.FX_RATE") == (
        "the invoice total in lari was not calculated: no NBG USD rate was found for 2026-09-17 "
        "(the imported rates cover 2026-09-10 … 2026-09-10); enter the rate or import newer rates"
    )


# --- the command line -------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


def test_the_cli_imports_a_file_and_reports_counts_only(
    tmp_path: Path, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rates = [
        {"date": "2026-09-10", "currency": "USD", "quantity": 1, "rate": "2.6104"},
        {"date": "2026-09-10", "currency": "EUR", "quantity": 1, "rate": "3.0300"},
    ]
    assert cli.main(["load-fx-rates", str(_json(tmp_path, rates))]) == 0
    out = capsys.readouterr().out
    assert "2 added, 0 changed, 0 unchanged; 2 currencies; 2026-09-10 … 2026-09-10" in out
    assert "2.6104" not in out
    assert cli.main(["fx-rates-status"]) == 0
    status = capsys.readouterr().out
    assert "EUR  2026-09-10 … 2026-09-10  (1 days)" in status
    assert "USD  2026-09-10 … 2026-09-10  (1 days)" in status
    assert (data_dir / "fx_rates.db").is_file()


def test_the_cli_refuses_a_bad_file_and_imports_nothing(
    tmp_path: Path, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = [CLIENT[0], {**CLIENT[1], "currency": "GEL"}]
    assert cli.main(["load-fx-rates", str(_json(tmp_path, bad))]) == 2
    assert "refused, nothing imported - rate 2:" in capsys.readouterr().err
    assert not (data_dir / "fx_rates.db").exists()
    assert cli.main(["load-fx-rates", str(tmp_path / "missing.json")]) == 2
    assert cli.main(["fx-rates-status"]) == 0
    assert "no exchange rates imported" in capsys.readouterr().out


# --- rates typed on the report's form ---------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "read"),
    [
        ("2.6104", "2.6104"),
        ("2,6104", "2.6104"),
        (" 2.61 ", "2.61"),
        ("0.018123", "0.018123"),
        ("999.999999", "999.999999"),
        ("0", None),
        ("-2.6", None),
        ("1000", None),
        ("2.6104123", None),
        ("2,610.4", None),
        ("2.6.1", None),
        ("abc", None),
        ("", None),
        (None, None),
        ("1" * 20, None),
    ],
)
def test_a_typed_rate_reads_as_people_type_it(typed: str | None, read: str | None) -> None:
    from procurement_agent.checks.fx_invoice import parse_typed_rate

    assert parse_typed_rate(typed) == (Decimal(read) if read else None)


def test_typed_rates_come_from_the_link_and_the_form_and_go_back_into_a_link() -> None:
    from procurement_agent.checks.fx_invoice import format_entered_rates, parse_entered_rates

    link = "USD:2026-09-10:2.6104,USD:2026-09-17:2.6081"
    typed = parse_entered_rates(link)
    assert typed == {("USD", SEP_10): Decimal("2.6104"), ("USD", SEP_17): Decimal("2.6081")}
    assert format_entered_rates(typed) == link
    # the form's field wins over the link's value for the same day
    both = parse_entered_rates(link, ["USD:2026-09-10"], ["2.70"])
    assert both[("USD", SEP_10)] == Decimal("2.70")
    # what does not read is left out
    junk = "GEL:2026-09-10:1,usd:2026-09-10:2.6,USD:2026-02-30:2.6,USD:2026-09-10:,nonsense"
    assert parse_entered_rates(junk) == {}
    many = ",".join(f"EUR:2026-08-{d:02d}:3.0{d:02d}" for d in range(1, 31))
    assert len(parse_entered_rates(many)) == 20


def test_the_summary_names_the_days_and_the_rate_each_got(provider: TableRateProvider) -> None:
    entered = EnteredRateProvider({("USD", SEP_17): Decimal("2.6090")}, provider)
    summary = check_aoa(aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=entered).summary
    assert summary["fx_days"] == "2026-09-10,2026-09-17"
    assert (summary["fx_rate_2026-09-10"], summary["fx_source_2026-09-10"]) == ("2.6104", "NBG")
    assert (summary["fx_rate_2026-09-17"], summary["fx_source_2026-09-17"]) == ("2.6090", ENTERED)


# --- the act's own dates and other currencies -------------------------------------------------


def test_an_advance_dated_after_the_act_is_a_warning_on_the_advance(
    provider: TableRateProvider,
) -> None:
    from tests.unit.test_fx_invoice import _usd_act

    act = _usd_act(advance=" ავანსი 1 000 აშშ დოლარი გადახდილია 2026 წლის 20 სექტემბერს.")
    report = check_aoa(aoa_text=act, contract_text=USD_CONTRACT, rates=provider)
    [late] = [
        f for f in report.findings if f.rule_id == "AOA.FX_INVOICE" and f.severity == "warning"
    ]
    assert late.message_en.startswith("the advance is dated 2026-09-20, after the act's date")
    assert late.expected is None  # which of the two dates is wrong is not the check's to say
    assert late.quote is not None and "1 000" in late.quote
    # an advance paid before the act raises nothing of the kind
    assert not [
        f
        for f in check_aoa(aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=provider).findings
        if f.severity == "warning"
    ]


def test_an_entered_advance_dated_after_the_act_is_a_warning_too(
    provider: TableRateProvider,
) -> None:
    from procurement_agent.checks.fx_invoice import AdvanceInput

    entered = AdvanceInput(amount=Decimal(1000), paid_on=date(2026, 9, 25))
    report = check_aoa(
        aoa_text=USD_ACT, contract_text=USD_CONTRACT, rates=provider, advance=entered
    )
    assert [f.severity for f in report.findings if f.rule_id == "AOA.FX_INVOICE"] == [
        "info",
        "warning",
    ]


def _act_in(fee: str, day: str = "10 სექტემბერ") -> str:
    from tests.unit.test_fx_invoice import _usd_act

    return _usd_act(fee=fee, advance="", day=day)


def test_an_act_in_swiss_francs_is_invoiced_at_the_franc_rate(tmp_path: Path) -> None:
    db = _table(
        tmp_path, [{"date": "2026-09-10", "currency": "CHF", "quantity": 1, "rate": "3.2765"}]
    )
    act = _act_in("საფასური შეადგენს 10 000 (ათი ათასი) შვეიცარიულ ფრანკს.")
    report = check_aoa(aoa_text=act, rates=TableRateProvider(db))
    assert report.summary["currency"] == "CHF"
    assert "10,000.00 CHF × NBG rate of 2026-09-10 (3.2765) = 32,765.00 GEL" in (
        _note(report).message_en
    )
    assert report.summary["invoice_gel"] == "32765.00"


def test_an_act_in_yen_takes_the_rate_of_one_yen_from_the_rate_of_a_hundred(
    tmp_path: Path,
) -> None:
    db = _table(
        tmp_path, [{"date": "2026-09-10", "currency": "JPY", "quantity": 100, "rate": "1.8123"}]
    )
    act = _act_in("საფასური შეადგენს 1 000 000 (ერთი მილიონი) იაპონურ იენს.")
    report = check_aoa(aoa_text=act, rates=TableRateProvider(db))
    assert report.summary["currency"] == "JPY"
    assert "1,000,000.00 JPY × NBG rate of 2026-09-10 (0.018123) = 18,123.00 GEL" in (
        _note(report).message_en
    )
