"""Tests for opencashflow.cli's basic credit-card CLI handlers:
cmd_creditcard_list/edit/cupo/map. `add` stays app-specific (owner
resolution via --username/interactive fallback -- see
GenericCommandExtensionPoints' own docstring) and has no engine-level test
here.

Direct ORM + direct handler calls (SimpleNamespace for args), same style
as test_creditcard_statements.py -- capsys captures stdout/stderr for
assertions.
"""
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.cli import cmd_creditcard_cupo, cmd_creditcard_edit, cmd_creditcard_list, cmd_creditcard_map
from opencashflow.credit_card import CreditCard
from opencashflow.models import Base, CashflowSheet, SheetPeriod, SheetRow, SheetSection

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def sheet(db):
    s = CashflowSheet(user_id=TEST_USER_ID, name="Tarjeta CLI Test Sheet", currency="CLP",
                       horizon_months=1, base_period=date(2026, 1, 1))
    db.add(s)
    db.flush()
    db.add(SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 1, 1), label="P0", sort_order=0))
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def row(db, sheet):
    section = SheetSection(sheet_id=sheet.id, name="Financiamiento", section_type="financing")
    db.add(section)
    db.flush()
    r = SheetRow(section_id=section.id, name="Pago tarjeta de crédito", row_type="input", sign="negative")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@pytest.fixture()
def card(db):
    c = CreditCard(
        user_id=TEST_USER_ID, name="Tarjeta CLI", bank="Banco X", credit_limit=1_000_000.0,
        current_balance=0.0, currency="CLP", closing_day=25, due_day=28, interest_rate=0.1,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


_EDIT_DEFAULTS = dict(
    name=None, bank=None, credit_limit=None, closing_day=None, due_day=None,
    interest_rate=None, minimum_payment_rate=None, currency=None, last4=None,
    expiration=None, network=None, holder_name=None, status=None,
)


def _edit_args(card_arg, **overrides):
    kwargs = {**_EDIT_DEFAULTS, "card": card_arg, **overrides}
    return NS(**kwargs)


# ---------------------------------------------------------------------------
# cmd_creditcard_list
# ---------------------------------------------------------------------------

def test_cmd_creditcard_list_prints_card(db, card, capsys):
    cmd_creditcard_list(db, NS(user_id=None))
    out = capsys.readouterr().out
    assert "Tarjeta CLI" in out
    assert "(sin mapear -- corre 'creditcard map')" in out


def test_cmd_creditcard_list_filters_by_user_id(db, card, capsys):
    cmd_creditcard_list(db, NS(user_id=999))
    assert "No hay tarjetas" in capsys.readouterr().out


def test_cmd_creditcard_list_empty(db, capsys):
    cmd_creditcard_list(db, NS(user_id=None))
    assert "No hay tarjetas de crédito registradas." in capsys.readouterr().out


def test_cmd_creditcard_list_shows_mapping_once_mapped(db, card, sheet, row, capsys):
    card.mapped_sheet_id = sheet.id
    card.mapped_row_id = row.id
    db.commit()
    cmd_creditcard_list(db, NS(user_id=None))
    out = capsys.readouterr().out
    assert f"planilla #{sheet.id} / fila #{row.id}" in out


# ---------------------------------------------------------------------------
# cmd_creditcard_edit
# ---------------------------------------------------------------------------

def test_cmd_creditcard_edit_updates_and_reports_changes(db, card, capsys):
    cmd_creditcard_edit(db, _edit_args("Tarjeta CLI", bank="Banco Y"))
    out = capsys.readouterr().out
    assert "bank: Banco X -> Banco Y" in out
    db.refresh(card)
    assert card.bank == "Banco Y"


def test_cmd_creditcard_edit_no_changes(db, card, capsys):
    cmd_creditcard_edit(db, _edit_args("Tarjeta CLI"))
    assert "Sin cambios" in capsys.readouterr().out


def test_cmd_creditcard_edit_not_found_exits(db, capsys):
    with pytest.raises(SystemExit):
        cmd_creditcard_edit(db, _edit_args("no existe"))
    assert "No se encontró" in capsys.readouterr().err


def test_cmd_creditcard_edit_by_substring(db, card, capsys):
    cmd_creditcard_edit(db, _edit_args("cli", credit_limit=2_000_000.0))
    db.refresh(card)
    assert card.credit_limit == 2_000_000.0


# ---------------------------------------------------------------------------
# cmd_creditcard_map
# ---------------------------------------------------------------------------

def test_cmd_creditcard_map_sets_mapping(db, card, sheet, row, capsys):
    cmd_creditcard_map(db, NS(card="Tarjeta CLI", sheet_id=sheet.id, row="Pago tarjeta de crédito"))
    out = capsys.readouterr().out
    assert f"planilla #{sheet.id}" in out
    db.refresh(card)
    assert card.mapped_sheet_id == sheet.id
    assert card.mapped_row_id == row.id


def test_cmd_creditcard_map_reports_previous_mapping(db, card, sheet, row, capsys):
    card.mapped_sheet_id = sheet.id
    card.mapped_row_id = row.id
    db.commit()
    other_row = SheetRow(section_id=row.section_id, name="Otra fila", row_type="input", sign="negative")
    db.add(other_row)
    db.commit()
    db.refresh(other_row)

    cmd_creditcard_map(db, NS(card="Tarjeta CLI", sheet_id=sheet.id, row="Otra fila"))
    out = capsys.readouterr().out
    assert f"antes: planilla #{sheet.id} / fila #{row.id}" in out


# ---------------------------------------------------------------------------
# cmd_creditcard_cupo
# ---------------------------------------------------------------------------

def test_cmd_creditcard_cupo_no_card_no_mapped_exits(db, card, capsys):
    with pytest.raises(SystemExit):
        cmd_creditcard_cupo(db, NS(card=None, unit="1"))
    assert "No hay tarjetas mapeadas" in capsys.readouterr().err


def test_cmd_creditcard_cupo_by_explicit_card(db, card, capsys):
    cmd_creditcard_cupo(db, NS(card="Tarjeta CLI", unit="1"))
    out = capsys.readouterr().out
    assert "Tarjeta CLI" in out
    assert "Real ahora" in out
    assert "Para planificar" in out
    # Fresh card, no charges/statements -- full credit_limit available both ways.
    assert "1.000.000" in out


def test_cmd_creditcard_cupo_uses_mapped_cards_when_no_card_given(db, card, sheet, row, capsys):
    card.mapped_sheet_id = sheet.id
    card.mapped_row_id = row.id
    db.commit()
    cmd_creditcard_cupo(db, NS(card=None, unit="1"))
    assert "Tarjeta CLI" in capsys.readouterr().out


def test_cmd_creditcard_cupo_flags_non_active_status(db, card, capsys):
    card.status = "blocked"
    db.commit()
    cmd_creditcard_cupo(db, NS(card="Tarjeta CLI", unit="1"))
    assert "estado=blocked" in capsys.readouterr().out
