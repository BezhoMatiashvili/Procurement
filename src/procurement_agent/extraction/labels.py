"""Human-readable labels and prompt descriptions for each fact type (English / Georgian)."""

from __future__ import annotations

from procurement_agent.extraction.schemas import FactType

LABELS: dict[FactType, tuple[str, str]] = {
    FactType.price: ("Unit price", "ერთეულის ფასი"),
    FactType.total_amount: ("Total amount", "ჯამური ღირებულება"),
    FactType.vat_status: ("VAT", "დღგ"),
    FactType.quantity: ("Quantity", "რაოდენობა"),
    FactType.delivery_deadline: ("Delivery deadline", "მიწოდების ვადა"),
    FactType.execution_period: ("Execution period", "შესრულების ვადა"),
    FactType.payment_terms: ("Payment terms", "გადახდის პირობები"),
    FactType.warranty_period: ("Warranty period", "საგარანტიო ვადა"),
    FactType.validity_period: ("Validity period", "მოქმედების ვადა"),
    FactType.penalty: ("Penalty", "პირგასამტეხლო"),
    FactType.contract_date: ("Contract date", "ხელშეკრულების თარიღი"),
    FactType.contract_number: ("Contract number", "ხელშეკრულების ნომერი"),
    FactType.party_name: ("Party name", "მხარის დასახელება"),
    FactType.party_id_code: ("Identification code", "საიდენტიფიკაციო კოდი"),
    FactType.party_director: ("Director / signatory", "დირექტორი / ხელმომწერი"),
    FactType.party_address: ("Legal address", "იურიდიული მისამართი"),
    FactType.bank_account: ("Bank account", "საბანკო რეკვიზიტები"),
    FactType.cadastral_code: ("Cadastral code", "საკადასტრო კოდი"),
    FactType.project_name: ("Project name", "პროექტის დასახელება"),
    FactType.subject: ("Subject", "ხელშეკრულების საგანი"),
    FactType.amount: ("Amount (unclassified)", "თანხა"),
    FactType.date: ("Date (unclassified)", "თარიღი"),
    FactType.percentage: ("Percentage (unclassified)", "პროცენტი"),
    FactType.duration: ("Duration (unclassified)", "ვადა"),
    FactType.id_code: ("ID code", "კოდი"),
    FactType.other: ("Other", "სხვა"),
}

# One line per type, shown to the LLM so it knows what each type means.
PROMPT_DESCRIPTIONS: dict[FactType, str] = {
    FactType.price: "unit price of a product/service (ფასი, ერთეულის ღირებულება)",
    FactType.total_amount: "total contract/offer value (სულ, ჯამი, საერთო ღირებულება)",
    FactType.vat_status: "whether amounts include VAT and the VAT rate (დღგ)",
    FactType.quantity: "quantity of goods/works with unit (რაოდენობა, ცალი, კომპლექტი)",
    FactType.delivery_deadline: "delivery date or period (მიწოდების ვადა)",
    FactType.execution_period: "period to complete works/services (შესრულების ვადა)",
    FactType.payment_terms: (
        "how and when payment is made: advance %, days after invoice (გადახდის პირობები)"
    ),
    FactType.warranty_period: "warranty duration (საგარანტიო ვადა)",
    FactType.validity_period: "offer/contract validity (მოქმედების ვადა, ძალაშია)",
    FactType.penalty: "penalties for delay or breach (პირგასამტეხლო, ჯარიმა, საურავი)",
    FactType.contract_date: "date the contract/offer was signed or issued (თარიღი)",
    FactType.contract_number: "contract/offer/tender reference number (№)",
    FactType.party_name: "legal name of a party/company (შპს, სს, LLC, Ltd)",
    FactType.party_id_code: "company identification/tax code, 9 or 11 digits (ს/კ)",
    FactType.party_director: "director or authorised signatory name (დირექტორი, წარმომადგენელი)",
    FactType.party_address: "registered legal address of a party (იურიდიული მისამართი)",
    FactType.bank_account: (
        "bank name, SWIFT/bank code and IBAN account number (საბანკო რეკვიზიტები, ა/ა)"
    ),
    FactType.cadastral_code: (
        "cadastral code of the property/project, e.g. 01.02.03.456 (საკადასტრო კოდი)"
    ),
    FactType.project_name: "name of the project the work belongs to (პროექტი)",
    FactType.subject: "what the contract/act is for, one phrase (საგანი, მომსახურება)",
    FactType.amount: "any other monetary amount",
    FactType.date: "any other date",
    FactType.percentage: "any other percentage",
    FactType.duration: "any other duration",
    FactType.id_code: "any other identification code",
    FactType.other: "other relevant procurement fact",
}


def label(ft: FactType) -> str:
    en, ka = LABELS[ft]
    return f"{en} / {ka}"
