"""Defects the fifth red-team round found in the act check's fee, tax-wording and bank rules.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R5.md``) and the behaviour the
spec asks for, and every widened reading has a near miss beside it that must still not match.
Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and account invented.
"""

from __future__ import annotations

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.contract_terms import amount_roles, tax_phrases
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


def _roles(text: str) -> list[tuple[float, str | None]]:
    return [(r.money.value, r.role) for r in amount_roles(text)]


_FEE_LINE = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."


def _fee_line(line: str) -> str:
    assert _FEE_LINE in ACT
    return ACT.replace(_FEE_LINE, line)


def _after_fee(sentence: str) -> str:
    return _fee_line(f"{_FEE_LINE} {sentence}")


# --- AOA-R5-02: the fee's remainder once the advance is deducted is a part of the fee ------------

_ADVANCE_CLAUSE = (
    "4.2. „შემკვეთი“ „შემსრულებელს“ გადაუხდის ავანსს ღირებულების 30%-ის ოდენობით; ყოველთვიური "
    "ანაზღაურებიდან ავანსი გამოიქვითება პროპორციულად.\n4.3."
)
#: The contract pays a 30% advance, deducted from each payment: of the 100 lari fee, 70 is paid.
_ADVANCE_CONTRACT = CONTRACT.replace("4.3.", _ADVANCE_CLAUSE, 1)


class TestTheRemainderAfterTheAdvance:
    _CLEAN = (
        "ავანსის (30%) გამოქვითვით ანაზღაურებას ექვემდებარება 70 (სამოცდაათი) ლარი.",
        "ავანსის გამოქვითვით ანაზღაურებას ექვემდებარება 70 ლარი.",
        "გამოქვითული ავანსი: 30 ლარი; გადასახდელი თანხა: 70 ლარი.",
        "აქედან გამოიქვითება ავანსი 30 ლარი, ხოლო ასანაზღაურებელია 70 ლარი.",
        "ავანსის თანხის გამოკლებით გადასახდელია 70 ლარი.",
    )

    def test_each_reads_as_the_remainder(self) -> None:
        for sentence in self._CLEAN:
            assert _roles(sentence)[-1] == (70.0, "remainder"), sentence

    def test_a_correct_act_stays_clean(self) -> None:
        for sentence in self._CLEAN:
            report = _check(_after_fee(sentence), _ADVANCE_CONTRACT)
            assert _substantive(report) == [], (sentence, _substantive(report))

    def test_a_share_the_act_alone_states_is_enough(self) -> None:
        """The advance stated only as a share, in the act, with a contract that has none."""
        sentence = "ავანსის (30%) გამოქვითვით ანაზღაურებას ექვემდებარება 70 ლარი."
        assert _substantive(_check(_after_fee(sentence))) == []

    def test_the_remainder_alone_is_the_fee_in_part(self) -> None:
        """An act that states only what is paid after the advance, against a 30% advance."""
        act = _fee_line("ავანსის გამოქვითვით ანაზღაურებას ექვემდებარება 70 ლარი.")
        assert _substantive(_check(act, _ADVANCE_CONTRACT)) == []

    def test_a_remainder_that_does_not_add_up_is_still_reported(self) -> None:
        """Near miss: 75 is neither the fee less 30 nor the fee less 30%."""
        for sentence in (
            "ავანსის (30%) გამოქვითვით ანაზღაურებას ექვემდებარება 75 ლარი.",
            "გამოქვითული ავანსი: 30 ლარი; გადასახდელი თანხა: 75 ლარი.",
        ):
            found = [
                f.quote for f in _of(_check(_after_fee(sentence), _ADVANCE_CONTRACT), "AOA.FEE")
            ]
            assert found == ["75 ლარი"], sentence

    def test_a_deduction_of_anything_but_the_advance_is_no_remainder(self) -> None:
        """Near misses: a tax deducted (a natural person's net fee), "to be paid in all", the
        advance's own deduction figure, and a deduction in an earlier sentence."""
        net = _roles("საშემოსავლო გადასახადის გამოქვითვით ასანაზღაურებელია 900 ლარი.")
        assert net[-1][1] in (None, "price")
        assert _roles("სულ ასანაზღაურებელია: 150 ლარი.")[-1][1] in (None, "price")
        assert _roles("ავანსის გამოქვითვის თანხა შეადგენს 30 ლარს.")[-1][1] != "remainder"
        later = _roles("ავანსი გამოიქვითება პროპორციულად. საფასური შეადგენს 100 ლარს.")
        assert later[-1][1] in (None, "price")
        # the advance named after the payable word is the advance; the fee after the remainder,
        # in a clause of its own, is the fee
        assert _roles("ავანსი გამოიქვითება ყოველთვიურად; გადასახდელი ავანსი: 30 ლარი.") == [
            (30.0, "advance")
        ]
        both = _roles("ავანსის გამოქვითვით გადასახდელია 70 ლარი, ხოლო საფასური შეადგენს 100 ლარს.")
        assert both[0] == (70.0, "remainder")
        assert both[1][1] in (None, "price")


# --- AOA-R5-08: the value of the services "already performed" is the act's own fee ---------------


class TestAlreadyPerformedIsTheFee:
    _WORDINGS = (
        "უკვე შესრულებული მომსახურების ღირებულება შეადგენს {f} გადასახადების ჩათვლით.",
        "ამ აქტით უკვე შესრულებული მომსახურების ღირებულება შეადგენს {f} გადასახადების ჩათვლით.",
        "საანგარიშო თვეში უკვე შესრულებული მომსახურების ღირებულება შეადგენს {f} გადასახადების "
        "ჩათვლით.",
    )

    def test_a_correct_fee_is_clean(self) -> None:
        for w in self._WORDINGS:
            report = _check(_fee_line(w.format(f="100 (ასი) ლარს")))
            assert _substantive(report) == [], w

    def test_a_slip_is_reported_and_corrected(self) -> None:
        for w in self._WORDINGS:
            found = _of(_check(_fee_line(w.format(f="110 ლარს"))), "AOA.FEE")
            assert [(f.severity, f.quote, f.expected) for f in found] == [
                ("error", "110 ლარს", "100 ლარს")
            ], w

    def test_paid_before_is_still_a_running_figure(self) -> None:
        """Near misses: "უკვე" before "paid" keeps the documented running-figure reading."""
        assert _roles("უკვე გადახდილი თანხა შეადგენს 200 ლარს.") == [(200.0, "running")]
        assert _roles("უკვე ანაზღაურებულია 200 ლარი.") == [(200.0, "running")]
        assert _roles("დღემდე შესრულებული მომსახურების ღირებულება 300 ლარი.") == [
            (300.0, "running")
        ]

    def test_an_act_whose_only_figure_is_running_gets_the_skip_note(self) -> None:
        """GOLD_SPEC 3: a running figure is not compared and AOA.FEE leaves a skip note naming
        it - never a 'no fee could be read' warning."""
        act = _fee_line("დღემდე შესრულებული მომსახურების ღირებულება შეადგენს 300 ლარს.")
        report = _check(act)
        assert _of(report, "AOA.FEE") == []
        notes = [s.reason_en for s in report.skipped if s.rule_id == "AOA.FEE"]
        assert len(notes) == 1
        assert "300 ლარს" in notes[0]

    def test_an_act_with_no_figure_still_gets_the_warning(self) -> None:
        """Near miss: no amount at all is still 'no fee could be read'."""
        act = _fee_line("საფასური მითითებულია დანართში.")
        assert [f.severity for f in _of(_check(act), "AOA.FEE")] == ["warning"]


# --- AOA-R5-09: "დღგ-ჩათვლით", VAT hyphen-joined to "including" -----------------------------------


class TestVatHyphenJoined:
    _INCLUDED = ("დღგ-ჩათვლით", "დ.ღ.გ.-ჩათვლით", "დ.ღ.გ-ჩათვლით", "დღგ-ს ჩათვლით")

    def test_each_says_vat_is_included(self) -> None:
        for w in self._INCLUDED:
            kinds = [p.kind for p in tax_phrases(f"100 ლარს {w}.")]
            assert kinds == ["inclusion"], w

    def test_the_fee_beside_it_is_still_the_fee(self) -> None:
        for w in self._INCLUDED:
            assert _roles(f"საფასური შეადგენს 100 ლარს {w}.") == [(100.0, "price")], w

    def test_a_vat_registered_entrepreneur_s_act_is_clean(self) -> None:
        for w in self._INCLUDED:
            act = _fee_line(f"საფასური შეადგენს 100 (ასი) ლარს {w}.")
            report = _check(act, contractor_type="individual_entrepreneur_vat")
            assert _of(report, "AOA.TAX_WORDING") == [], w

    def test_hyphen_joined_without_is_an_exclusion(self) -> None:
        """Near miss: the same joint before "without" excludes VAT, and offends the rule on the
        fee's own words."""
        assert [p.kind for p in tax_phrases("100 ლარს დღგ-გარეშე.")] == ["exclusion"]
        act = _fee_line("საფასური შეადგენს 100 (ასი) ლარს დღგ-გარეშე.")
        found = _of(_check(act, contractor_type="individual_entrepreneur_vat"), "AOA.TAX_WORDING")
        assert [(f.severity, f.quote) for f in found] == [("error", "დღგ-გარეშე")]

    def test_vat_closing_its_clause_is_still_the_tax(self) -> None:
        """Near misses: the tax named after its figure, dotted or not, and a VAT number label."""
        assert _roles("საიდანაც 18 ლარი დ.ღ.გ.") == [(18.0, "tax")]
        assert _roles("მათ შორის 18 ლარი დღგ.") == [(18.0, "tax")]
        assert _roles("მათ შორის 18 ლარი დ.ღ.გ. საფასური შეადგენს 100 ლარს.")[0] == (18.0, "tax")
        assert [p.kind for p in tax_phrases("დღგ-ს გადამხდელის ნომერი: 123")] == []


# --- AOA-R5-13: a BIC with a real branch code against the contract's 8-character BIC -------------


class TestBicBranchCode:
    def _bank(self, report: CheckReport) -> list[tuple[str, str | None, str | None]]:
        return [(f.severity, f.actual, f.expected) for f in _of(report, "AOA.BANK")]

    def test_a_branch_of_the_same_bank_is_a_warning_naming_it(self) -> None:
        report = _check(ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22TBS;"))
        assert self._bank(report) == [("warning", "AAAAGE22TBS", "AAAAGE22")]
        [f] = _of(report, "AOA.BANK")
        assert "TBS" in f.message_en and "TBS" in f.message_ka
        assert f.span is not None
        assert ACT.replace("AAAAGE22;", "AAAAGE22TBS;")[f.span[0] : f.span[1]] == "AAAAGE22TBS"

    def test_the_primary_office_form_stays_clean(self) -> None:
        report = _check(ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22XXX;"))
        assert _of(report, "AOA.BANK") == []

    def test_another_bank_or_another_branch_is_still_an_error(self) -> None:
        """Near misses: another institution with a branch, the contract's branch against the
        act's head office (not decided: an error as before), and two different branches."""
        other = _check(ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: BBBBGE22TBS;"))
        assert [s for s, _, _ in self._bank(other)] == ["error"]
        branch_contract = CONTRACT.replace("AAAAGE22,", "AAAAGE22TBS,")
        assert [s for s, _, _ in self._bank(_check(ACT, branch_contract))] == ["error"]
        two = _check(
            ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22VAK;"), branch_contract
        )
        assert [s for s, _, _ in self._bank(two)] == ["error"]

    def test_the_branch_beside_its_account_pairs_with_the_bank(self) -> None:
        """With two banks in the contract the codes are also paired with accounts: the branch
        form of the contractor's bank is that bank's code, not another account's."""
        contract = CONTRACT.replace(
            "4.3. ანგარიშსწორება:",
            "4.2. „შემკვეთის“ რეკვიზიტები: სს „ნიმუში ბანკი“, ბანკის კოდი: CCCCGE22,\n"
            "ანგარიშის ნომერი: GE95TB0000000123456789.\n4.3. ანგარიშსწორება:",
        )
        act = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: AAAAGE22TBS;")
        bank = [
            *_of(_check(act, contract), "AOA.BANK"),
            *_of(_check(act, contract), "AOA.BANK_PAIRING"),
        ]
        assert [(f.rule_id, f.severity) for f in bank] == [("AOA.BANK", "warning")]
        # near miss: the customer's bank beside the contractor's account is still wrong
        wrong = ACT.replace("ბანკის კოდი: AAAAGE22;", "ბანკის კოდი: CCCCGE22;")
        assert [f.severity for f in _of(_check(wrong, contract), "AOA.BANK")] == ["error"]
