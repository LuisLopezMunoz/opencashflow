"""Tests for opencashflow.creditcard_ops -- card create/edit/map, charges,
and real statements (header upsert + lines). cli.py aliases edit_card as
_do_edit_card and `creditcard map` calls map_card."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow import cli
from opencashflow.credit_card import CreditCard, CreditCardCharge, CreditCardStatement, CreditCardStatementLine
from opencashflow.creditcard_ops import (
    add_charge,
    add_statement_line,
    create_card,
    create_statement,
    edit_card,
    map_card,
    upsert_statement,
)
from opencashflow.models import Base, CashflowSheet, SheetRow, SheetSection

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def card(db):
    return create_card(db, TEST_USER_ID, "Visa", Decimal("1000000"), bank="Banco X", currency="clp",
                       closing_day=25, due_day=5)


def _sheet_row(db, name="Planilla"):
    sheet = CashflowSheet(user_id=TEST_USER_ID, name=name, currency="CLP", horizon_months=1,
                          base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    section = SheetSection(sheet_id=sheet.id, name="Tarjetas", section_type="expense")
    db.add(section)
    db.flush()
    row = SheetRow(section_id=section.id, name="Visa - Total a pagar", row_type="input")
    db.add(row)
    db.commit()
    return sheet, row


_HEADER = dict(closing_date=date(2026, 2, 25), due_date=date(2026, 3, 5))


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def test_cli_aliases_are_the_creditcard_ops_functions():
    assert cli._do_edit_card is edit_card
    assert cli.map_card is map_card


def test_create_card_normalizes_currency_and_expiration(db):
    c = create_card(db, TEST_USER_ID, "Master", 500000.5, currency="usd", expiration_date=date(2029, 7, 19))
    assert (c.currency, c.expiration_date, c.credit_limit) == ("USD", date(2029, 7, 1), Decimal("500000.50"))
    assert c.current_balance == Decimal("0")


@pytest.mark.parametrize("kwargs,match", [
    ({"closing_day": 40}, "closing_day"),
    ({"due_day": 0}, "due_day"),
    ({"last4": "4111111111111111"}, "últimos 4"),
    ({"last4": "12a4"}, "últimos 4"),
    ({"status": "frozen"}, "status"),
])
def test_create_card_rejects_bad_input_and_writes_nothing(db, kwargs, match):
    with pytest.raises(ValueError, match=match):
        create_card(db, TEST_USER_ID, "Mala", Decimal("1"), **kwargs)
    assert db.query(CreditCard).count() == 0


def test_edit_card_applies_only_real_changes_and_normalizes(db, card):
    changes = edit_card(db, card, credit_limit=1000000, interest_rate=0.24, expiration_date=date(2030, 1, 15))
    assert set(changes) == {"interest_rate", "expiration_date"}
    assert (card.interest_rate, card.expiration_date) == (Decimal("0.24"), date(2030, 1, 1))


@pytest.mark.parametrize("kwargs", [
    {"name": "Otra", "closing_day": 32},
    {"name": "Otra", "last4": "123"},
    {"name": "Otra", "status": "frozen"},
])
def test_edit_card_validates_everything_before_touching_the_card(db, card, kwargs):
    with pytest.raises(ValueError):
        edit_card(db, card, **kwargs)
    assert card.name == "Visa"
    assert not db.dirty


def test_edit_card_none_clears_a_nullable_field(db, card):
    assert edit_card(db, card, bank=None) == {"bank": ("Banco X", None)}


def test_map_card_points_the_card_at_a_row_and_reports_the_previous_mapping(db, card):
    sheet, row = _sheet_row(db)
    assert map_card(db, card, sheet, row) == (None, None)
    other_sheet, other_row = _sheet_row(db, "Otra")
    assert map_card(db, card, other_sheet, other_row) == (sheet.id, row.id)
    assert (card.mapped_sheet_id, card.mapped_row_id) == (other_sheet.id, other_row.id)


def test_map_card_refuses_a_row_of_another_sheet(db, card):
    sheet, _ = _sheet_row(db)
    _, foreign_row = _sheet_row(db, "Otra")
    with pytest.raises(ValueError, match="no pertenece"):
        map_card(db, card, sheet, foreign_row)
    assert card.mapped_row_id is None


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------

def test_add_charge_stores_the_whole_purchase_and_never_touches_current_balance(db, card):
    charge = add_charge(db, card, 300000, description="Notebook", installments=3,
                        first_statement_period=date(2026, 4, 17), charge_date=date(2026, 3, 30))
    assert (charge.amount, charge.installments, charge.installments_paid) == (Decimal("300000"), 3, 0)
    assert charge.first_statement_period == date(2026, 4, 1)
    assert charge.charge_date == date(2026, 3, 30)
    db.refresh(card)
    assert card.current_balance == Decimal("0")


def test_add_charge_defaults_to_today_and_no_statement_period(db, card):
    charge = add_charge(db, card, Decimal("5000"))
    assert (charge.charge_date, charge.first_statement_period) == (date.today(), None)


def test_add_charge_refuses_an_inactive_card(db, card):
    edit_card(db, card, status="blocked")
    with pytest.raises(ValueError, match="blocked"):
        add_charge(db, card, Decimal("1"))
    assert db.query(CreditCardCharge).count() == 0


def test_add_charge_refuses_zero_installments(db, card):
    with pytest.raises(ValueError, match="installments"):
        add_charge(db, card, Decimal("1"), installments=0)


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

def test_upsert_statement_creates_a_manual_statement_in_the_card_currency(db, card):
    result = upsert_statement(db, card, date(2026, 2, 14), values={
        "new_balance": Decimal("131000"), "total_payment_due": Decimal("131000"), "previous_balance": 100000,
    }, **_HEADER)
    s = result.statement
    assert (result.created, result.changes) == (True, {})
    assert (s.billing_period, s.source, s.currency, s.created_by) == (date(2026, 2, 1), "manual", "CLP", TEST_USER_ID)
    assert s.previous_balance == Decimal("100000")


def test_upsert_statement_updates_in_place_only_what_was_given(db, card):
    first = upsert_statement(db, card, date(2026, 2, 1), values={
        "new_balance": 131000, "total_payment_due": 131000, "remaining_principal_installments": 900000,
    }, notes="original", **_HEADER).statement
    second = upsert_statement(db, card, date(2026, 2, 1), values={
        "new_balance": 131000, "total_payment_due": 135000,
    }, **_HEADER)
    assert second.created is False
    assert second.statement.id == first.id
    assert second.changes == {"total_payment_due": (Decimal("131000"), Decimal("135000"))}
    assert second.statement.remaining_principal_installments == Decimal("900000")
    assert second.statement.notes == "original"
    assert db.query(CreditCardStatement).count() == 1


def test_upsert_statement_needs_the_two_required_amounts(db, card):
    with pytest.raises(ValueError, match="total_payment_due"):
        upsert_statement(db, card, date(2026, 2, 1), values={"new_balance": 1}, **_HEADER)
    assert db.query(CreditCardStatement).count() == 0


def test_statement_values_must_be_known_columns(db, card):
    with pytest.raises(ValueError, match="desconocidos"):
        create_statement(db, card, date(2026, 2, 1), values={"new_balance": 1, "total_payment_due": 1, "bogus": 1},
                         currency="CLP", source="manual", **_HEADER)


@pytest.fixture()
def statement(db, card):
    return upsert_statement(db, card, date(2026, 3, 1), values={"new_balance": 20000, "total_payment_due": 20000},
                            closing_date=date(2026, 3, 25), due_date=date(2026, 4, 5)).statement


def _line(db, statement, **kwargs):
    base = dict(line_type="charge", description="Cuota Notebook", transaction_date=date(2026, 1, 10),
                amount=Decimal("20000"))
    base.update(kwargs)
    line = add_statement_line(db, statement, **base)
    db.commit()
    return line


def test_add_statement_line_links_an_installment_of_a_tracked_charge(db, card, statement):
    charge = add_charge(db, card, Decimal("240000"), installments=12)
    line = _line(db, statement, installment_number=3, installment_total=12, charge_id=charge.id)
    assert (line.statement_id, line.charge_id, line.installment_number) == (statement.id, charge.id, 3)


@pytest.mark.parametrize("kwargs,match", [
    ({"installment_number": 3}, "van juntos"),
    ({"installment_number": 13, "installment_total": 12}, "inválida"),
    ({"installment_number": 0, "installment_total": 12}, "inválida"),
    ({"charge_id": 999}, "No existe el cargo"),
    ({"line_type": "refund"}, "line_type"),
])
def test_add_statement_line_rejects_bad_input_and_adds_nothing(db, statement, kwargs, match):
    with pytest.raises(ValueError, match=match):
        add_statement_line(db, statement, line_type=kwargs.pop("line_type", "charge"), description="X",
                           transaction_date=date(2026, 1, 1), amount=Decimal("1"), **kwargs)
    db.commit()
    assert db.query(CreditCardStatementLine).count() == 0


def test_add_statement_line_refuses_another_cards_charge(db, card, statement):
    other = create_card(db, TEST_USER_ID, "Otra", Decimal("1"))
    foreign = add_charge(db, other, Decimal("1"))
    with pytest.raises(ValueError, match="no pertenece"):
        add_statement_line(db, statement, line_type="charge", description="X", transaction_date=date(2026, 1, 1),
                           amount=Decimal("1"), charge_id=foreign.id)


def test_add_statement_line_refuses_the_same_installment_twice(db, card, statement):
    charge = add_charge(db, card, Decimal("240000"), installments=12)
    _line(db, statement, installment_number=3, installment_total=12, charge_id=charge.id)
    with pytest.raises(ValueError, match="no se puede duplicar"):
        _line(db, statement, installment_number=3, installment_total=12, charge_id=charge.id)
