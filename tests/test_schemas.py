"""Tests for opencashflow.schemas -- previously a 302-line module with ZERO
consumers anywhere in this repo (confirmed by grep) and no test file of its
own, which is exactly how PeriodOut's bug went unnoticed: its mode="before"
validator set data._is_current (with a leading underscore) via
object.__setattr__, but the declared field is named is_current (no
underscore) -- model_validate(a_real_SheetPeriod) raised
"is_current Field required" on every call, always, for as long as the
module existed.

schemas.py is not wired into any real call site yet (see its own module
comment) -- these tests exist so validating each schema against a REAL ORM
instance (model_validate, not a hand-typed dict) is exercised at all, which
is what would have caught the PeriodOut bug for free the day it was
written, and so a future consumer (e.g. an HTTP API layer) can trust this
module's shapes actually match the ORM models they're meant to mirror.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.credit_card import CreditCard, CreditCardCharge, CreditCardStatement, CreditCardStatementLine
from opencashflow.creditcard_statements import compute_instant_statement, sync_period
from opencashflow.engine import CellResult
from opencashflow.models import (
    Base,
    CashflowSheet,
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    SheetSection,
)
from opencashflow.overrides import clear_override, set_override
from opencashflow.period_close import close_period, reopen_period
from opencashflow.record_ops import clear_record, record_history, set_record, undo_record
from opencashflow.schemas import (
    CellActualUpdate,
    CellComputedOut,
    CellOverrideCreate,
    CellOverrideOut,
    CloseReportOut,
    CreditCardChargeCreate,
    CreditCardChargeOut,
    CreditCardCreate,
    CreditCardOut,
    CreditCardStatementOut,
    InstantStatementOut,
    MatrixOut,
    MatrixRowOut,
    MatrixSectionOut,
    OverrideClearResultOut,
    OverrideWriteResultOut,
    PeriodOut,
    PeriodsBackfillCreate,
    RealValuesOut,
    RecordClearResultOut,
    RecordHistoryEntryOut,
    RecordSetResultOut,
    RecordUndoResultOut,
    ReopenReportOut,
    RowCreate,
    RowOut,
    RowUpdate,
    SectionCreate,
    SectionOut,
    SectionWithRowsOut,
    SheetCreate,
    SheetDetailOut,
    SheetOut,
    SyncReportOut,
    WalletCreate,
    WalletMovementResultOut,
    WalletMovementUndoResultOut,
    WalletOut,
)
from opencashflow.seed import seed_sheet
from opencashflow.wallet import Wallet
from opencashflow.wallet_movements import do_wallet_movement_add, do_wallet_movement_undo

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _sheet(db) -> CashflowSheet:
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Schema Test", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.commit()
    db.refresh(sheet)
    return sheet


def _section(db, sheet) -> SheetSection:
    section = SheetSection(sheet_id=sheet.id, name="S", section_type="custom")
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def _row(db, section) -> SheetRow:
    row = SheetRow(section_id=section.id, name="A")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _period(db, sheet, period_date) -> SheetPeriod:
    period = SheetPeriod(sheet_id=sheet.id, period_date=period_date, label="P", sort_order=0)
    db.add(period)
    db.commit()
    db.refresh(period)
    return period


# ---------------------------------------------------------------------------
# PeriodOut -- the bug (and its fix)
# ---------------------------------------------------------------------------

def test_period_out_validates_against_a_real_sheet_period_and_computes_is_current(db):
    sheet = _sheet(db)
    today = datetime.utcnow()
    current_month_period = _period(db, sheet, datetime(today.year, today.month, 1))

    out = PeriodOut.model_validate(current_month_period)

    assert out.id == current_month_period.id
    assert out.is_current is True


def test_period_out_is_current_false_for_a_different_month(db):
    sheet = _sheet(db)
    past_period = _period(db, sheet, datetime(2020, 1, 1))

    out = PeriodOut.model_validate(past_period)

    assert out.is_current is False


# ---------------------------------------------------------------------------
# *Out schemas with a direct ORM counterpart -- model_validate against a
# real, committed instance (not a hand-typed dict standing in for one).
# ---------------------------------------------------------------------------

def test_sheet_out_validates_against_a_real_cashflow_sheet(db):
    sheet = _sheet(db)
    out = SheetOut.model_validate(sheet)
    assert (out.id, out.name, out.currency) == (sheet.id, sheet.name, sheet.currency)


def test_section_out_validates_against_a_real_sheet_section(db):
    section = _section(db, _sheet(db))
    out = SectionOut.model_validate(section)
    assert (out.id, out.name, out.section_type) == (section.id, section.name, section.section_type)


def test_row_out_validates_against_a_real_sheet_row(db):
    row = _row(db, _section(db, _sheet(db)))
    out = RowOut.model_validate(row)
    assert (out.id, out.name, out.row_type, out.sign) == (row.id, row.name, row.row_type, row.sign)


def test_cell_override_out_validates_against_a_real_cell_override(db):
    row = _row(db, _section(db, _sheet(db)))
    cell = SheetCell(row_id=row.id, period_id=1)
    db.add(cell)
    db.flush()
    override = CellOverride(cell_id=cell.id, value=Decimal("100"), override_type="manual_value",
                             created_by=TEST_USER_ID)
    db.add(override)
    db.commit()
    db.refresh(override)

    out = CellOverrideOut.model_validate(override)
    assert (out.id, out.value, out.override_type) == (override.id, Decimal("100"), "manual_value")


# ---------------------------------------------------------------------------
# *Create/*Update -- pure input schemas, no ORM counterpart to validate
# against; exercise their own custom validators instead.
# ---------------------------------------------------------------------------

def test_sheet_create_accepts_minimal_input():
    spec = SheetCreate(name="X", base_period=datetime(2026, 1, 1))
    assert spec.currency == "USD"
    assert spec.horizon_months == 12


def test_section_create_and_row_create_and_row_update_accept_minimal_input():
    assert SectionCreate(name="S").section_type == "custom"
    assert RowCreate(name="A").row_type == "input"
    assert RowUpdate().name is None  # every field optional, for a partial PATCH


def test_periods_backfill_create_requires_months():
    with pytest.raises(Exception):
        PeriodsBackfillCreate()  # months has no default
    assert PeriodsBackfillCreate(months=3).months == 3


def test_cell_override_create_derives_override_type_from_which_field_is_set():
    assert CellOverrideCreate(value=Decimal("1")).override_type == "manual_value"
    assert CellOverrideCreate(custom_rule={"type": "constant", "value": 1}).override_type == "manual_rule"
    assert CellOverrideCreate().override_type == "lock"


def test_cell_override_create_rejects_both_value_and_custom_rule():
    with pytest.raises(Exception, match="not both"):
        CellOverrideCreate(value=Decimal("1"), custom_rule={"type": "constant", "value": 1})


def test_cell_actual_update_accepts_partial_fields():
    update = CellActualUpdate(paid_value=Decimal("500"))
    assert "paid_value" in update.model_fields_set
    assert "accrued_value" not in update.model_fields_set


# ---------------------------------------------------------------------------
# CellComputedOut -- field-for-field the engine's own CellResult dataclass,
# validated directly off one (no hand-built field-by-field construction).
# ---------------------------------------------------------------------------

def test_cell_computed_out_validates_directly_against_an_engine_cell_result():
    cr = CellResult(row_id=1, period_id=2, projected_value=Decimal("100"), accrued_value=Decimal("100"),
                    paid_value=Decimal("40"), pending_value=Decimal("60"), effective_source="rule")
    out = CellComputedOut.model_validate(cr)
    assert (out.row_id, out.period_id, out.projected_value, out.pending_value) == (1, 2, Decimal("100"), Decimal("60"))
    assert out.error is None


# ---------------------------------------------------------------------------
# Composite schemas -- built from the real pieces above, to catch a
# mismatch in how they nest (not just each leaf schema in isolation).
# ---------------------------------------------------------------------------

def test_section_with_rows_out_and_sheet_detail_out_compose_from_real_instances(db):
    sheet = _sheet(db)
    section = _section(db, sheet)
    row = _row(db, section)
    period = _period(db, sheet, sheet.base_period)

    section_out = SectionWithRowsOut.model_validate(section)
    assert section_out.rows[0].id == row.id  # SheetSection.rows relationship, read live

    detail = SheetDetailOut(
        **SheetOut.model_validate(sheet).model_dump(),
        periods=[PeriodOut.model_validate(period)],
        sections=[section_out],
    )
    assert detail.periods[0].id == period.id
    assert detail.sections[0].rows[0].id == row.id


def test_matrix_out_composes_from_real_instances(db):
    sheet = _sheet(db)
    section = _section(db, sheet)
    row = _row(db, section)
    period = _period(db, sheet, sheet.base_period)

    matrix_row = MatrixRowOut(row=RowOut.model_validate(row), cells=[
        CellComputedOut(row_id=row.id, period_id=period.id, projected_value=Decimal("100"),
                         actual_value=None, accrued_value=None, paid_value=None, pending_value=None,
                         variance=None, effective_source="rule"),
    ])
    matrix_section = MatrixSectionOut(section=SectionOut.model_validate(section), rows=[matrix_row])
    matrix = MatrixOut(sheet=SheetOut.model_validate(sheet), periods=[PeriodOut.model_validate(period)],
                        sections=[matrix_section])

    assert matrix.sections[0].rows[0].cells[0].projected_value == Decimal("100")


# ---------------------------------------------------------------------------
# Result DTOs -- each validated directly off the REAL value the core
# function it mirrors returns (a dataclass holding ORM rows), via
# model_validate(result). Exactly what a route handler will do, so a shape
# mismatch here fails a test instead of a request.
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded(db):
    sheet = seed_sheet(db, user_id=TEST_USER_ID, months=3, base_period=date(2026, 1, 1))
    db.commit()
    periods = sorted(sheet.periods, key=lambda p: p.sort_order)

    def row(name):
        return db.query(SheetRow).join(SheetSection).filter(
            SheetSection.sheet_id == sheet.id, SheetRow.name == name,
        ).one()

    return sheet, periods, row


def test_record_result_dtos_validate_off_real_record_ops_results(db, seeded):
    sheet, periods, row = seeded
    sueldo, p1 = row("Sueldo líquido"), periods[0]

    set_result = set_record(db, sueldo, p1, accrued_value=Decimal("500"), created_by=TEST_USER_ID)
    out = RecordSetResultOut.model_validate(set_result)
    assert out.previous.accrued_value is None
    assert out.current.accrued_value == Decimal("500")
    assert out.entry.entry_kind == "record"
    assert out.period.id == p1.id

    history = [RecordHistoryEntryOut.model_validate(h) for h in record_history(db, sueldo, p1)]
    assert history[-1].is_current is True

    undo = RecordUndoResultOut.model_validate(undo_record(db, sueldo, p1, created_by=TEST_USER_ID))
    assert undo.reverted_to is None  # back to no recorded value
    assert undo.new_entry.entry_kind == "undo"

    set_record(db, sueldo, p1, paid_value=Decimal("1"), created_by=TEST_USER_ID)
    cleared = [RecordClearResultOut.model_validate(r) for r in clear_record(db, sueldo, [p1], created_by=TEST_USER_ID)]
    assert cleared[0].cleared is True
    assert cleared[0].previous.paid_value == Decimal("1")


def test_real_values_out_reads_a_positional_tuple():
    out = RealValuesOut.model_validate((Decimal("1"), None, Decimal("3")))
    assert (out.actual_value, out.accrued_value, out.paid_value) == (Decimal("1"), None, Decimal("3"))


def test_override_result_dtos_validate_off_real_overrides_results(db, seeded):
    sheet, periods, row = seeded
    sueldo, p1 = row("Sueldo líquido"), periods[0]

    written = set_override(db, sheet.id, sueldo, [p1], value=Decimal("999"), created_by=TEST_USER_ID)
    out = OverrideWriteResultOut.model_validate(written[0])
    assert out.override.value == Decimal("999")
    assert out.skipped is False

    cleared = OverrideClearResultOut.model_validate(clear_override(db, sheet.id, sueldo, [p1])[0])
    assert cleared.previous_override.value == Decimal("999")


def test_close_and_reopen_report_dtos_validate_off_real_period_close_reports(db, seeded):
    sheet, periods, _ = seeded

    close_out = CloseReportOut.model_validate(close_period(db, sheet, periods[0], TEST_USER_ID))
    assert close_out.period_id == periods[0].id
    assert isinstance(close_out.saldo_final_value, Decimal)

    reopen_out = ReopenReportOut.model_validate(reopen_period(db, sheet, periods[0]))
    assert reopen_out.period_id == periods[0].id


def test_wallet_dtos_validate_off_real_wallet_movement_results(db, seeded):
    sheet, periods, row = seeded
    wallet = Wallet(user_id=TEST_USER_ID, name="Cuenta", currency=sheet.currency, balance=Decimal("0"))
    db.add(wallet)
    db.commit()

    added = do_wallet_movement_add(db, wallet, sheet.id, row("Sueldo líquido"), periods[0], Decimal("1000"),
                                    note=None, created_by=TEST_USER_ID)
    db.commit()
    add_out = WalletMovementResultOut.model_validate(added)
    assert add_out.movement.amount == Decimal("1000")
    assert add_out.wallet_balance_after == Decimal("1000")

    undo_out = WalletMovementUndoResultOut.model_validate(
        do_wallet_movement_undo(db, added.movement, note=None, created_by=TEST_USER_ID)
    )
    db.commit()
    assert undo_out.reversal.reverses_movement_id == undo_out.original.id
    assert WalletOut.model_validate(wallet).balance == Decimal("0")


def test_credit_card_dtos_validate_off_real_rows_and_statements(db, seeded):
    sheet, periods, row = seeded
    card = CreditCard(user_id=TEST_USER_ID, name="Visa", credit_limit=Decimal("1000000"), currency=sheet.currency,
                      closing_day=25, due_day=5, interest_rate=Decimal("0.24"))
    db.add(card)
    db.flush()
    charge = CreditCardCharge(credit_card_id=card.id, amount=Decimal("120000"), charge_date=date(2026, 1, 10),
                              installments=3, first_statement_period=date(2026, 1, 1))
    statement = CreditCardStatement(
        credit_card_id=card.id, billing_period=date(2025, 12, 1), new_balance=Decimal("50000"),
        total_payment_due=Decimal("50000"), closing_date=date(2025, 12, 25), due_date=date(2026, 1, 5),
        currency=sheet.currency, source="manual",
    )
    db.add_all([charge, statement])
    db.flush()
    db.add(CreditCardStatementLine(statement_id=statement.id, line_type="charge", description="Compra",
                                   transaction_date=date(2025, 12, 10), amount=Decimal("50000")))
    db.commit()

    card_out = CreditCardOut.model_validate(card)
    assert card_out.interest_rate == Decimal("0.24")
    assert "current_balance" not in CreditCardOut.model_fields  # deprecated, deliberately not exposed

    assert CreditCardChargeOut.model_validate(charge).installments == 3
    statement_out = CreditCardStatementOut.model_validate(statement)
    assert statement_out.lines[0].description == "Compra"

    instant = InstantStatementOut.model_validate(compute_instant_statement(db, card, date(2026, 2, 1)))
    assert instant.card.id == card.id
    assert instant.is_real is False
    assert instant.lines[0].installment_number == 1

    report = sync_period(db, card, sheet, row("Pago tarjeta de crédito"), date(2026, 2, 1), TEST_USER_ID, dry_run=True)
    sync_out = SyncReportOut.model_validate(report)
    assert sync_out.branch == "projection"
    assert sync_out.dry_run is True


# ---------------------------------------------------------------------------
# New input DTOs -- their own boundary validation.
# ---------------------------------------------------------------------------

def test_credit_card_create_rejects_anything_but_exactly_four_digits_for_last4():
    assert CreditCardCreate(name="Visa", credit_limit=Decimal("1"), last4="1234").last4 == "1234"
    for bad in ("4111111111111111", "12a4", "123"):
        with pytest.raises(ValidationError):
            CreditCardCreate(name="Visa", credit_limit=Decimal("1"), last4=bad)


def test_credit_card_create_defaults_match_the_model():
    card = CreditCardCreate(name="Visa", credit_limit=Decimal("1"))
    assert (card.status, card.minimum_payment_rate, card.interest_rate) == ("active", Decimal("0.05"), Decimal("0"))


def test_credit_card_charge_create_rejects_zero_installments():
    with pytest.raises(ValidationError):
        CreditCardChargeCreate(amount=Decimal("1"), installments=0)


def test_wallet_create_accepts_minimal_input():
    wallet = WalletCreate(name="Efectivo")
    assert (wallet.wallet_type, wallet.balance) == ("cash", Decimal("0"))
