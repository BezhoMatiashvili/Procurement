"""The readers the act checks share: sentences, the role of each amount, where a fee's wording
is, the tax wording itself, and a contract's term, payment terms and advance. Synthetic text only.

The checks read flattened text (no line breaks), so every case here is a single line, the way
``DocumentView.text`` hands it over.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from procurement_agent.checks.amounts import money_hits
from procurement_agent.checks.contract_terms import (
    TermRange,
    amount_roles,
    fee_scope,
    read_advance,
    read_payment_terms,
    read_term,
    sentence_spans,
    tax_phrases,
)


def sentences(text: str) -> list[str]:
    return [text[a:b] for a, b in sentence_spans(text)]


def test_sentences_end_at_a_stop_followed_by_a_space() -> None:
    text = "ღირებულება შეადგენს 100 ლარს; ანგარიშსწორება ხდება 5 დღეში. დასასრული!"
    assert sentences(text) == [
        "ღირებულება შეადგენს 100 ლარს;",
        "ანგარიშსწორება ხდება 5 დღეში.",
        "დასასრული!",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "პირგასამტეხლო შეადგენს 0.5% დღეში, მაგრამ არაუმეტეს 10%-ისა.",
        "ღირებულება შეადგენს 48 750,00 ლარს და 2.5 ლარს საკომისიოდ.",
    ],
)
def test_a_decimal_or_a_percentage_is_never_a_sentence_end(text: str) -> None:
    assert sentences(text) == [text]


def test_a_clause_number_stays_with_its_own_clause() -> None:
    text = "ფასი 100 ლარი. 4.2.1. ანგარიშსწორება ხდება 5 დღეში."
    assert sentences(text) == ["ფასი 100 ლარი.", "4.2.1. ანგარიშსწორება ხდება 5 დღეში."]


def test_a_clause_number_opens_a_clause_after_a_flattened_heading() -> None:
    """The heading has no stop of its own: its line break was flattened away."""
    text = "მუხლი II. ხელშეკრულების საგნის შესრულება 2.1. „შემსრულებელი“ ვალდებულია შეასრულოს."
    assert sentences(text) == [
        "მუხლი II.",
        "ხელშეკრულების საგნის შესრულება",
        "2.1. „შემსრულებელი“ ვალდებულია შეასრულოს.",
    ]


def test_a_list_item_number_after_a_colon_keeps_its_item() -> None:
    text = "მომსახურების აღწერა: 1. გვერდის შექმნა. 2. ლოგოს დიზაინი."
    assert sentences(text) == ["მომსახურების აღწერა: 1. გვერდის შექმნა.", "2. ლოგოს დიზაინი."]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "იურიდიული მისამართი: ქ. თბილისი, რუსთაველის 12. შემდეგი წინადადება.",
            ["იურიდიული მისამართი: ქ. თბილისი, რუსთაველის 12.", "შემდეგი წინადადება."],
        ),
        ("მომსახურება 2026 წ. 1 აპრილიდან იწყება.", ["მომსახურება 2026 წ. 1 აპრილიდან იწყება."]),
    ],
)
def test_a_one_letter_abbreviation_is_not_a_sentence_end(text: str, expected: list[str]) -> None:
    assert sentences(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the letter before the stop ends a word: "დღგ-ს." closes its sentence
        ("თანხა მოიცავს დღგ-ს. შემდეგი.", ["თანხა მოიცავს დღგ-ს.", "შემდეგი."]),
        # a date or a cadastral code is not a clause number, so its stop is a sentence end
        ("აქტი გაფორმდა 30.04.2026. შესრულდა.", ["აქტი გაფორმდა 30.04.2026.", "შესრულდა."]),
        ("კოდი: 01.02.03.456. შემდეგი.", ["კოდი: 01.02.03.456.", "შემდეგი."]),
    ],
)
def test_what_looks_like_an_exception_but_is_a_sentence_end(text: str, expected: list[str]) -> None:
    assert sentences(text) == expected


def test_spans_index_the_text_and_leave_out_the_whitespace() -> None:
    text = "  პირველი.   მეორე  "
    assert sentence_spans(text) == [(2, 10), (13, 18)]
    assert sentences(text) == ["პირველი.", "მეორე"]
    assert sentence_spans("") == []
    assert sentence_spans("   ") == []


# --- the role each amount plays ------------------------------------------------------------------


def roles(text: str) -> list[tuple[float, str | None]]:
    return [(r.money.value, r.role) for r in amount_roles(text)]


class TestAmountRoles:
    def test_a_price_sentence_that_mentions_vat_stays_the_price(self) -> None:
        """The contract template names the tax in its price clause - after the figure, as what
        the price includes. That must not make the price a tax amount."""
        template = (
            "4.1. ღირებულება შეადგენს 100 (ასი) ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს."
        )
        assert roles(template) == [(100.0, "price")]
        assert roles("საფასური შეადგენს 100 ლარს დღგ-ს ჩათვლით.") == [(100.0, "price")]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "ღირებულება შეადგენს 118 ლარს, მათ შორის დღგ 18 ლარი.",
                [(118.0, "price"), (18.0, "tax")],
            ),
            ("Price: 100.00 GEL; VAT amount: 18.00 GEL.", [(100.0, "price"), (18.0, "tax")]),
            ("დღგ-ს თანხა (18%): 18 ლარი.", [(18.0, "tax")]),
            # the tax, then the verb that states it
            (
                "საფასური შეადგენს 118 ლარს, საიდანაც დღგ შეადგენს 18 ლარს.",
                [(118.0, "price"), (18.0, "tax")],
            ),
            (
                "საფასური შეადგენს 118 ლარს, მათ შორის დღგ-ს თანხა შეადგენს 18 ლარს.",
                [(118.0, "price"), (18.0, "tax")],
            ),
            ("The fee is 118 GEL; VAT is 18 GEL.", [(118.0, "price"), (18.0, "tax")]),
            ("The fee is 118 GEL, of which VAT is 18 GEL.", [(118.0, "price"), (18.0, "tax")]),
        ],
    )
    def test_a_tax_named_right_before_the_figure_makes_it_the_tax(
        self, text: str, expected: list[tuple[float, str]]
    ) -> None:
        assert roles(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "ღირებულება შეადგენს 118 ლარს, მათ შორის 18 ლარი დღგ.",
                [(118.0, "price"), (18.0, "tax")],
            ),
            ("Total: 118.00 GEL, of which 18.00 GEL VAT.", [(118.0, "price"), (18.0, "tax")]),
            ("მათ შორის 18 ლარი (დღგ).", [(18.0, "tax")]),
            ("მათ შორის 18 ლარი დღგ-ს სახით.", [(18.0, "tax")]),
            (
                "საფასური 118 ლარი (მათ შორის 18 ლარი დღგ) გადაიხდება 5 დღეში.",
                [(118.0, "price"), (18.0, "tax")],
            ),
        ],
    )
    def test_a_tax_named_right_after_the_figure_makes_it_the_tax(
        self, text: str, expected: list[tuple[float, str]]
    ) -> None:
        assert roles(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # the tax word goes on to say what the price includes or leaves out
            ("საფასური შეადგენს 100 ლარს დღგ-ს გარეშე.", [(100.0, "price")]),
            ("საფასური შეადგენს 100 ლარს (დღგ-ს ჩათვლით).", [(100.0, "price")]),
            ("საფასური 100 ლარი დღგ და სხვა გადასახადების ჩათვლით.", [(100.0, "price")]),
            ("The fee is 100 GEL VAT included.", [(100.0, "price")]),
            # the tax is added to the price, not stated as an amount
            ("The fee is 100 GEL + VAT.", [(100.0, "price")]),
            ("ღირებულება 100 ლარი დღგ-თი.", [(100.0, "price")]),
            # the tax word labels the figure after it
            ("ღირებულება 100 ლარი დღგ: 18 ლარი.", [(100.0, "price"), (18.0, "tax")]),
            # the tax word goes on to say how the tax is charged: the figure is still the price
            ("საფასური შეადგენს 100 ლარს დღგ-ს დარიცხვით.", [(100.0, "price")]),
            ("საფასური შეადგენს 100 ლარს დღგ-ს ჩართვით.", [(100.0, "price")]),
            ("The fee is 100 GEL VAT to be added.", [(100.0, "price")]),
        ],
    )
    def test_a_tax_word_after_a_price_that_qualifies_it_leaves_it_the_price(
        self, text: str, expected: list[tuple[float, str]]
    ) -> None:
        assert roles(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Total including VAT 118 GEL.",
            "Total incl. VAT 118 GEL.",
            "Total with VAT: 118 GEL.",
            "Price including 18% VAT: 118 GEL.",
            "The fee including VAT is 118 GEL.",
            "Price before VAT: 118 GEL.",
            "Price after VAT: 118 GEL.",
            "Price ex. VAT: 118 GEL.",
            # the verb states the price the tax is added to, not the tax
            "The fee plus VAT is 118 GEL.",
            "საფასური, რომელსაც ემატება დღგ შეადგენს 118 ლარს.",
        ],
    )
    def test_a_price_qualified_by_its_tax_is_not_the_tax(self, text: str) -> None:
        [(value, role)] = roles(text)
        assert (value, role != "tax") == (118.0, True)

    @pytest.mark.parametrize(
        "text",
        [
            "დამკვეთი უზრუნველყოფს 100 ლარის ანაზღაურებას.",
            "შემსრულებელი უზრუნველყოფს 100 ლარის ღირებულების მომსახურებას.",
        ],
    )
    def test_the_verb_to_ensure_is_no_guarantee(self, text: str) -> None:
        """ "Ensures" (უზრუნველყოფს) shares its stem with "security" (უზრუნველყოფა), and read as
        a guarantee the fee left the act altogether: "no fee found in the act"."""
        assert roles(text) == [(100.0, "price")]

    @pytest.mark.parametrize(
        ("text", "role"),
        [
            ("ავანსი შეადგენს 300 ლარს.", "advance"),
            ("ავანსის სახით გადახდილია 300 ლარი.", "advance"),
            ("Advance paid: 1,000 USD.", "advance"),
            ("დარჩენილი თანხა 700 ლარი გადაიხდება აქტის გაფორმებიდან 5 დღეში.", "remainder"),
            ("Remaining balance: 10,000 USD.", "remainder"),
            ("პირგასამტეხლო შეადგენს 50 ლარს ყოველ დღეზე.", "penalty"),
            ("საგარანტიო თანხა შეადგენს 20 ლარს.", "guarantee"),
            ("ხელშეკრულების შესრულების უზრუნველყოფა შეადგენს 500 ლარს.", "guarantee"),
        ],
    )
    def test_each_role_is_read_from_the_words_around_the_amount(self, text: str, role: str) -> None:
        [(_, found)] = roles(text)
        assert found == role

    def test_the_second_tranche_is_the_remainder(self) -> None:
        text = "პირველი ტრანში 300 ლარი, მეორე ტრანში 700 ლარი."
        assert roles(text) == [(300.0, "advance"), (700.0, "remainder")]

    def test_a_price_and_an_advance_in_one_sentence_keep_their_own_roles(self) -> None:
        text = "საფასური შეადგენს 1 000 ლარს, მათ შორის ავანსი 300 ლარი."
        assert roles(text) == [(1000.0, "price"), (300.0, "advance")]

    def test_a_role_is_read_inside_the_amounts_own_sentence(self) -> None:
        assert roles("ავანსი არ გაიცემა. თანხა 100 ლარი.") == [(100.0, None)]

    def test_a_penalty_is_named_rather_than_dropped(self) -> None:
        text = "პირგასამტეხლო შეადგენს 500 ₾ ყოველ ვადაგადაცილებულ დღეზე."
        assert money_hits(text) == []  # the document comparison's reading leaves it out
        assert [(h.value, h.currency) for h in money_hits(text, penalties=True)] == [(500.0, "GEL")]
        assert roles(text) == [(500.0, "penalty")]

    def test_every_amount_keeps_its_currency_and_where_it_is_written(self) -> None:
        text = "Fee: 11,000 USD. Advance paid: 1,000 USD. Remaining balance: 10,000 USD."
        found = amount_roles(text)
        assert [(r.money.currency, r.role) for r in found] == [
            ("USD", "price"),
            ("USD", "advance"),
            ("USD", "remainder"),
        ]
        assert all(text[slice(*r.money.span)] == r.money.raw for r in found)


# --- where a fee's wording is ---------------------------------------------------------------------


def _span_of(text: str, needle: str) -> tuple[int, int]:
    start = text.index(needle)
    return start, start + len(needle)


class TestFeeScope:
    def test_the_fees_sentence_and_the_next_one(self) -> None:
        """The next one because a table's next cell is where "NET" is often written."""
        text = "შესავალი. საფასური 100 ლარი. NET. ბოლო წინადადება."
        lo, hi = fee_scope(sentence_spans(text), _span_of(text, "100 ლარი"))
        assert text[lo:hi] == "საფასური 100 ლარი. NET."

    def test_the_last_sentence_has_no_next_one(self) -> None:
        text = "შესავალი. საფასური 100 ლარი."
        lo, hi = fee_scope(sentence_spans(text), _span_of(text, "100 ლარი"))
        assert text[lo:hi] == "საფასური 100 ლარი."

    def test_a_long_sentence_is_capped_and_still_holds_the_fee(self) -> None:
        text = "სიტყვა " * 100 + "100 ლარი " + "სიტყვა " * 100 + "დასასრული."
        start, end = _span_of(text, "100 ლარი")
        lo, hi = fee_scope(sentence_spans(text), (start, end))
        assert hi - lo <= 400
        assert lo <= start and end <= hi
        assert start - lo <= 100  # most of the room is after the figure, where the wording goes


# --- tax wording ---------------------------------------------------------------------------------


def phrases(text: str) -> list[tuple[str, str]]:
    return [(p.kind, p.text) for p in tax_phrases(text)]


class TestTaxPhrases:
    @pytest.mark.parametrize(
        "text",
        ["100 ლარი NET", "NET-ით", "ნეტო ღირებულებით", "ნეტოს", "netto", "(NET)", "100 ლარი NET."],
    )
    def test_net_is_read_as_a_word(self, text: str) -> None:
        assert [p.kind for p in tax_phrases(text)] == ["net"]

    @pytest.mark.parametrize(
        "text",
        [
            "network",
            "Netherlands",
            "ნეტარი",
            "ინტერნეტ მომსახურება",
            "internet service",
            # a software platform, an e-mail address and a web address
            "ASP.NET აპლიკაციის შექმნა",
            "info@net.ge",
            "www.net.ge",
        ],
    )
    def test_net_inside_another_word_is_not_net(self, text: str) -> None:
        assert tax_phrases(text) == []

    @pytest.mark.parametrize(
        ("text", "phrase", "names"),
        [
            ("100 ლარს გადასახადების ჩათვლით", "გადასახადების ჩათვლით", set()),
            ("100 ლარს დღგ-ს ჩათვლით", "დღგ-ს ჩათვლით", {"vat"}),
            ("თანხა დღგ-ს ჩათვლითაა", "დღგ-ს ჩათვლით", {"vat"}),
            ("საშემოსავლო გადასახადის ჩათვლით", "საშემოსავლო გადასახადის ჩათვლით", {"income"}),
            (
                "დამატებული ღირებულების გადასახადის ჩათვლით",
                "დამატებული ღირებულების გადასახადის ჩათვლით",
                {"vat"},
            ),
            (
                "ანგარიშსწორება ითვალისწინებს გადასახადებს",
                "ითვალისწინებს გადასახადებს",
                set(),
            ),
            ("100 GEL including all taxes", "including all taxes", set()),
            ("100 GEL, VAT included", "VAT included", {"vat"}),
            ("100 GEL inclusive of VAT", "inclusive of VAT", {"vat"}),
            ("the gross amount of 100 GEL", "gross amount", set()),
            # "taking the tax into account"
            ("100 ლარს დღგ-ს გათვალისწინებით", "დღგ-ს გათვალისწინებით", {"vat"}),
            ("100 ლარს გადასახადების გათვალისწინებით", "გადასახადების გათვალისწინებით", set()),
            # the contracts' boilerplate: "all taxes provided for by the legislation of Georgia"
            (
                "თანხა მოიცავს საქართველოს კანონმდებლობით გათვალისწინებულ ყველა გადასახადს",
                "მოიცავს საქართველოს კანონმდებლობით გათვალისწინებულ ყველა გადასახადს",
                set(),
            ),
        ],
    )
    def test_inclusions(self, text: str, phrase: str, names: set[str]) -> None:
        [found] = tax_phrases(text)
        assert (found.kind, found.text, found.names) == ("inclusion", phrase, names)

    @pytest.mark.parametrize(
        ("text", "phrase", "names"),
        [
            (
                "საშემოსავლო და საპენსიო გადასახადების გარეშე",
                "საშემოსავლო და საპენსიო გადასახადების გარეშე",
                {"income", "pension"},
            ),
            ("საშემოსავლო გადასახადის გარეშე", "საშემოსავლო გადასახადის გარეშე", {"income"}),
            ("საპენსიო შენატანის გარეშე", "საპენსიო შენატანის გარეშე", {"pension"}),
            ("დღგ-ს გარეშე", "დღგ-ს გარეშე", {"vat"}),
            ("გადასახადის გამოკლებით", "გადასახადის გამოკლებით", set()),
            ("100 ლარი + დღგ", "+ დღგ", {"vat"}),
            ("თანხა არ მოიცავს გადასახადებს", "არ მოიცავს გადასახადებს", set()),
            ("100 GEL without VAT", "without VAT", {"vat"}),
            ("100 GEL, VAT not included", "VAT not included", {"vat"}),
            (
                "excluding income tax and pension contributions",
                "excluding income tax and pension contributions",
                {"income", "pension"},
            ),
            # "and" joins the two taxes; it is not a third word of what the phrase names
            (
                "საშემოსავლო გადასახადისა და საპენსიო შენატანის გარეშე",
                "საშემოსავლო გადასახადისა და საპენსიო შენატანის გარეშე",
                {"income", "pension"},
            ),
            (
                "თანხა არ მოიცავს საქართველოს კანონმდებლობით გათვალისწინებულ გადასახადებს",
                "არ მოიცავს საქართველოს კანონმდებლობით გათვალისწინებულ გადასახადებს",
                set(),
            ),
        ],
    )
    def test_exclusions(self, text: str, phrase: str, names: set[str]) -> None:
        [found] = tax_phrases(text)
        assert (found.kind, found.text, found.names) == ("exclusion", phrase, names)

    def test_the_template_price_clause_includes_the_taxes_and_names_vat(self) -> None:
        found = tax_phrases("100 ლარს, რომელიც მოიცავს გადასახადებს მათ შორის დღგ-ს.")
        assert [(p.kind, p.text) for p in found] == [
            ("inclusion", "მოიცავს გადასახადებს"),
            ("inclusion", "მათ შორის დღგ-ს"),
        ]
        assert found[1].names == {"vat"}

    def test_a_date_up_to_and_including_is_no_tax_wording(self) -> None:
        assert tax_phrases("შესრულდა 2026 წლის 30 აპრილის ჩათვლით") == []

    def test_an_exclusion_does_not_run_on_into_what_follows(self) -> None:
        """ "Without tax, other costs included": the tax is excluded, not included."""
        assert phrases("გადასახადის გარეშე, სხვა ხარჯების ჩათვლით") == [
            ("exclusion", "გადასახადის გარეშე")
        ]
        assert phrases("მოიცავს ყველა ხარჯს გადასახადების გარეშე") == [
            ("exclusion", "გადასახადების გარეშე")
        ]

    def test_net_of_taxes_is_net_and_an_exclusion(self) -> None:
        assert phrases("100 GEL net of taxes") == [("net", "net"), ("exclusion", "net of taxes")]

    @pytest.mark.parametrize(
        "text",
        [
            "VAT No.: GB123456789",
            "Tax ID: 12-3456789",
            "VAT-ID: DE123456789",
            "Tax-ID: 12-3456789",
            "TIN 12-345",
            "საგადასახადო ნომერი: 123",
            "გადასახადის გადამხდელის საიდენტიფიკაციო ნომერი: 1",
            "დღგ-ს გადამხდელის ნომერი 5",
        ],
    )
    def test_a_tax_number_label_is_not_tax_wording(self, text: str) -> None:
        assert tax_phrases(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "Each party pays its own taxes.",
            "საქართველოს საგადასახადო კოდექსის შესაბამისად",  # the Tax Code is a law, not a number
            # "provided for" (the adjective) is not "taking into account"
            "კანონმდებლობით გათვალისწინებული გადასახადები",
        ],
    )
    def test_any_other_tax_word_is_a_mention(self, text: str) -> None:
        assert [p.kind for p in tax_phrases(text)] == ["mention"]

    @pytest.mark.parametrize("text", ["taxi", "syntax", "private"])
    def test_letters_inside_another_word_are_no_mention(self, text: str) -> None:
        assert tax_phrases(text) == []

    @pytest.mark.parametrize("unit", ["the ", "tax and ", "vat, "])
    def test_a_page_of_tax_words_is_read_in_linear_time(self, unit: str) -> None:
        """Documents are untrusted input. The taxes an English phrase names were an unbounded
        run, so every word of a page of "tax and tax and ..." restarted a scan to its end:
        64 KB took 16 seconds, and twice the text four times as long."""
        started = time.perf_counter()
        tax_phrases(unit * 8_000)
        assert time.perf_counter() - started < 1.0


# --- the term ------------------------------------------------------------------------------------

#: A preamble - the place and the date - and the first party, as the contract template opens.
_PREAMBLE = (
    "ხელშეკრულება ქ. თბილისი 1 მარტი, 2026 წელი შპს „ალფა“, საიდენტიფიკაციო კოდი: 123456789 "
    "(შემდგომში „შემკვეთი“). "
)
_MAR_1, _SEP_1, _DEC_31 = date(2026, 3, 1), date(2026, 9, 1), date(2026, 12, 31)


class TestReadTerm:
    def test_the_performance_window_is_read_from_its_marked_dates(self) -> None:
        text = (
            _PREAMBLE
            + "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით."
        )
        term = read_term(text)
        assert term.performance == TermRange(start=_MAR_1, end=_SEP_1)
        assert term.validity is None

    @pytest.mark.parametrize(
        "clause",
        [
            "3.1. ხელშეკრულება მოქმედებს 01.03.2026-დან 31.12.2026-მდე.",
            "3.1. ხელშეკრულება ძალაში შედის 2026 წლის 1 მარტს და მოქმედებს 2026 წლის 31 "
            "დეკემბრამდე.",
            "3.1. ხელშეკრულება მოქმედებს 01.03.2026 - 31.12.2026.",
        ],
    )
    def test_a_validity_range_however_its_ends_are_marked(self, clause: str) -> None:
        assert read_term(_PREAMBLE + clause).validity == TermRange(start=_MAR_1, end=_DEC_31)

    def test_a_date_between_the_contract_and_the_verb_still_reads_the_clause(self) -> None:
        clause = "3.1. ხელშეკრულება 01.03.2026-დან მოქმედებს 31.12.2026-მდე."
        assert read_term(_PREAMBLE + clause).validity == TermRange(start=_MAR_1, end=_DEC_31)
        english = "The Contract, signed on 01.03.2026, is valid until 31 December 2026."
        assert read_term(_PREAMBLE + english).validity == TermRange(start=None, end=_DEC_31)

    def test_a_lone_date_after_the_word_for_the_term_is_its_last_day(self) -> None:
        term = read_term(_PREAMBLE + "3.1. ხელშეკრულების მოქმედების ვადა: 31.12.2026.")
        assert term.validity == TermRange(start=None, end=_DEC_31)

    def test_a_lone_date_that_is_the_signing_is_no_end(self) -> None:
        """A guessed last day would call every later act late."""
        clause = (
            "3.1. ხელშეკრულება გაფორმდა 2026 წლის 1 მარტს და მოქმედებს ვალდებულებების შესრულებამდე."
        )
        assert read_term(_PREAMBLE + clause).validity == TermRange(start=None, end=None)

    def test_validity_from_the_signing_is_open_ended(self) -> None:
        """The client's template: in force from signing until the obligations are fulfilled."""
        clause = (
            "3.1. „ხელშეკრულება“ ძალაში შედის მისი ხელმოწერის მომენტიდან და მოქმედებს „მხარეთა“ "
            "მიერ ნაკისრი ვალდებულებების სრულად და ჯეროვნად შესრულებამდე."
        )
        term = read_term(_PREAMBLE + clause)
        assert term.signed == _MAR_1
        assert term.validity == TermRange(start=_MAR_1, end=None)

    def test_english_clauses(self) -> None:
        text = (
            "The Contractor shall perform the Services from 1 March 2026 to 1 September 2026. "
            "The Contract is valid until 31 December 2026."
        )
        term = read_term(text)
        assert term.performance == TermRange(start=_MAR_1, end=_SEP_1)
        assert term.validity == TermRange(start=None, end=_DEC_31)

    def test_the_signing_date_is_the_one_before_the_first_party(self) -> None:
        assert read_term(_PREAMBLE).signed == _MAR_1
        # a date after the parties is some clause's, never the signing
        assert read_term("შპს „ალფა“ (შემდგომში „შემკვეთი“) 1 მარტი, 2026 წელი.").signed is None
        # and with no party at all there is no preamble to read it from
        assert read_term("1 მარტი, 2026 წელი.").signed is None

    @pytest.mark.parametrize(
        "clause",
        [
            # "including VAT" is no term, whatever date follows it
            "4.1. ღირებულება შეადგენს 100 ლარს დღგ-ს ჩათვლით. 2027 წლის 15 მარტი.",
            # a payment's date is not the day the contract ends, even worded as the term
            "4.2. საბოლოო ანგარიშსწორება მოხდება ხელშეკრულების მოქმედების ვადაში, 2027 წლის 15 "
            "მარტამდე.",
            # a guarantee's validity is the guarantee's
            "5.1. საბანკო გარანტია მოქმედებს 2027 წლის 31 მარტამდე.",
            # and a power of attorney's, a price list's or a duty's is theirs: only the contract,
            # named before the verb, is in force for the contract's term
            "5.2. წარმომადგენლის მინდობილობა მოქმედებს 2026 წლის 31 მარტამდე.",
            "5.3. ხელშეკრულებით განსაზღვრული ფასები მოქმედებს 2026 წლის 31 მარტის ჩათვლით.",
            "5.4. კონფიდენციალურობის ვალდებულება ძალაშია 2026 წლის 31 მარტამდე.",
            "5.5. The Contractor's insurance is valid until 31 March 2026.",
        ],
    )
    def test_what_is_not_the_contracts_term(self, clause: str) -> None:
        performance = "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან 2026 წლის 1 სექტემბრამდე. "
        term = read_term(_PREAMBLE + performance + clause)
        assert term.performance == TermRange(start=_MAR_1, end=_SEP_1)
        assert term.validity is None or term.validity.end is None

    def test_a_contract_with_no_term_clause_has_none(self) -> None:
        term = read_term(_PREAMBLE + "4.1. ღირებულება შეადგენს 100 ლარს.")
        assert (term.validity, term.performance) == (None, None)


# --- the payment terms ---------------------------------------------------------------------------


def _deadlines(text: str) -> list[tuple[int, str, str | None, str]]:
    return [(d.count, d.unit, d.qualifier, d.raw) for d in read_payment_terms(text).deadlines]


def _method(text: str) -> tuple[str, str] | None:
    found = read_payment_terms(text).method
    return (found.method, found.text) if found else None


class TestReadPaymentTerms:
    def test_the_templates_deadline_and_method(self) -> None:
        text = (
            "4.2.1. ღირებულება გადაიხდება ყოველთვიურად მიღება-ჩაბარების აქტის გაფორმების "
            "თარიღიდან 5 (ხუთი) სამუშაო დღის ვადაში. 4.3. ანგარიშსწორება მოხდება „შემსრულებლის“ "
            "შემდეგ საბანკო ანგარიშზე გადარიცხვით: სს „ბანკი“."
        )
        assert _deadlines(text) == [(5, "day", "working", "5 (ხუთი) სამუშაო დღის")]
        assert _method(text) == ("non_cash", "საბანკო ანგარიშზე")

    def test_the_notice_period_is_no_payment_deadline(self) -> None:
        """The template gives five days' notice of termination as well as five days to pay."""
        text = (
            "3.3. დამკვეთს უფლება აქვს შეწყვიტოს ხელშეკრულება შეწყვეტამდე 5 (ხუთი) კალენდარული "
            "დღით ადრე შეტყობინების საფუძველზე. აღნიშნულ შემთხვევაში ანაზღაურება განხორციელდება "
            "შესრულების პროპორციულად. ჯარიმა გადაიხდება 10 დღის ვადაში."
        )
        assert _deadlines(text) == []

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("ანგარიშსწორება მოხდება 10 დღეში.", [(10, "day", None, "10 დღეში")]),
            ("ღირებულება გადაიხდება არაუგვიანეს 3 დღისა.", [(3, "day", None, "3 დღისა")]),
            (
                "Payment shall be made within 5 business days.",
                [(5, "day", "working", "5 business days")],
            ),
            (
                "The fee is paid 30 calendar days after the invoice.",
                [(30, "day", "calendar", "30 calendar days")],
            ),
            # a duration that is no time limit: what the fee is for
            ("ღირებულება გადაიხდება 6 თვიანი მომსახურებისთვის ყოველთვიურად.", []),
            # "განმავლობაში" (during) is a time limit in days, as the client's act writes it ...
            (
                "საფასური გადაიხდება აქტის გაფორმების თარიღიდან 5 (ხუთი) სამუშაო დღის "
                "განმავლობაში.",
                [(5, "day", "working", "5 (ხუთი) სამუშაო დღის")],
            ),
            ("ანგარიშსწორება მოხდება 10 დღის განმავლობაში.", [(10, "day", None, "10 დღის")]),
            # ... or counted from an event; otherwise it says how long the work ran
            (
                "თანხა გადაიხდება ხელშეკრულების გაფორმებიდან 1 თვის განმავლობაში.",
                [(1, "month", None, "1 თვის")],
            ),
            (
                "საანგარიშო პერიოდში (1 თვის განმავლობაში) გაწეული მომსახურების საფასური "
                "გადაიხდება 5 დღეში.",
                [(5, "day", None, "5 დღეში")],
            ),
            ("ღირებულება გადაიხდება ყოველთვიურად, 6 თვის განმავლობაში.", []),
            ("The fee for 6 months of service is paid monthly.", []),
        ],
    )
    def test_a_duration_is_a_deadline_when_written_as_one(
        self, text: str, expected: list[tuple[int, str, str | None, str]]
    ) -> None:
        assert _deadlines(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("ანგარიშსწორება მოხდება უნაღდო ფორმით.", ("non_cash", "უნაღდო")),
            ("ანგარიშსწორება მოხდება ნაღდი ანგარიშსწორებით.", ("cash", "ნაღდი")),
            ("თანხა გადაიხდება გადარიცხვით.", ("non_cash", "გადარიცხვით")),
            ("Payment in cash.", ("cash", "cash")),
            ("The fee is payable by non-cash transfer.", ("non_cash", "non-cash")),
            # both named: the document has said neither
            ("ანგარიშსწორება მოხდება ნაღდი ან უნაღდო ფორმით.", None),
            # outside a sentence about paying, an account is only an account
            ("შემსრულებლის საბანკო ანგარიში: GE00XX0000000000000000.", None),
        ],
    )
    def test_the_payment_method(self, text: str, expected: tuple[str, str] | None) -> None:
        """ "უნაღდო" (non-cash) holds the letters of "ნაღდ" (cash): it must never read as cash."""
        assert _method(text) == expected


# --- the advance ---------------------------------------------------------------------------------


class TestReadAdvance:
    def test_an_acts_advance_is_dated_by_the_payment(self) -> None:
        text = "ავანსი 1 000 აშშ დოლარი გადახდილია 2026 წლის 10 სექტემბერს."
        [paid] = read_advance(text, in_act=True).payments
        assert (paid.money.value, paid.money.currency, paid.paid_on) == (
            1000.0,
            "USD",
            date(2026, 9, 10),
        )

    def test_a_contract_plans_an_advance_and_dates_nothing(self) -> None:
        """A date in a contract's advance clause is a deadline, not a payment."""
        text = "ავანსი 1 000 აშშ დოლარი გადაიხდება 2026 წლის 10 სექტემბრამდე."
        [planned] = read_advance(text, in_act=False).payments
        assert (planned.money.value, planned.paid_on) == (1000.0, None)

    def test_an_advance_as_a_share_of_the_price(self) -> None:
        """The VAT rate in the same sentence is no share of the advance."""
        advance = read_advance(
            "4.2. ავანსი შეადგენს ღირებულების 30%-ს, დღგ 18%-ის ჩათვლით.", in_act=False
        )
        assert [s.percent for s in advance.shares] == [30.0]
        assert advance.stated

    @pytest.mark.parametrize(
        "text",
        [
            "ავანსი არ გაიცემა.",
            "4.2.2. ავანსის გადახდა გათვალისწინებული არ არის.",
            "ღირებულება გადაიხდება ავანსის გარეშე.",
            "ავანსი გადახდილი არ ყოფილა.",
            "No advance payment is made under this Contract.",
            # an act's own ways of saying none was paid
            "ავანსი არ განხორციელებულა.",
            "ავანსი არ მიუღია.",
            "ავანსი არ ყოფილა.",
            "ავანსი არ გადაცემულა.",
            "ავანსი: არა.",
            "ავანსი - არ არის.",
            "Advance: none.",
            "Advance payment: none.",
            # an advance of nothing is none
            "ავანსი: 0 ლარი.",
            "ავანსი: 0.",
        ],
    )
    def test_an_advance_ruled_out(self, text: str) -> None:
        advance = read_advance(text, in_act=False)
        assert advance.mentioned and advance.excluded and not advance.stated

    @pytest.mark.parametrize(
        "text",
        [
            # a denial of something else about the advance leaves the advance in place
            "ავანსის დაბრუნება არ მოხდება.",
            # a denial of how much, not of whether
            "ავანსი, რომელიც არ აღემატება 30%-ს, გადაიხდება ხელმოწერიდან 5 დღეში.",
            "ავანსი 30%-ზე მეტი არ გაიცემა.",
            "ავანსი: არა უმეტეს 30%-ისა.",
            # the act's own name holds "მიღება" (receipt): a denial beside it is of something else
            "მიღება-ჩაბარების აქტის ხელმოწერისას ავანსი არ ბრუნდება.",
        ],
    )
    def test_a_negation_about_something_else_rules_nothing_out(self, text: str) -> None:
        advance = read_advance(text, in_act=False)
        assert advance.stated and not advance.excluded

    @pytest.mark.parametrize(
        "text",
        [
            "წინასწარი შეტყობინების გარეშე.",  # a prior notice
            "The Contractor shall notify the Client 5 days in advance.",  # a when, not a what
            "ღირებულება გადაიხდება 5 დღეში.",
        ],
    )
    def test_no_advance_is_spoken_of(self, text: str) -> None:
        advance = read_advance(text, in_act=True)
        assert not advance.mentioned and not advance.stated and advance.anchor is None


@pytest.mark.parametrize(
    "unit",
    [
        "ხელშეკრულება მოქმედებს 2026 წლის 31 დეკემბრამდე. ",
        "ავანსი 1 000 ლარი გადახდილია 2026 წლის 10 სექტემბერს. ",
        "ღირებულება გადაიხდება 5 დღეში, უნაღდო ანგარიშსწორებით. ",
    ],
)
def test_the_term_and_payment_readers_take_a_page_of_clauses_in_stride(unit: str) -> None:
    """Documents are untrusted input: each clause's dates are looked up, not scanned for."""
    text = unit * 1_500
    started = time.perf_counter()
    read_term(text)
    read_payment_terms(text)
    read_advance(text, in_act=True)
    assert time.perf_counter() - started < 3.0
