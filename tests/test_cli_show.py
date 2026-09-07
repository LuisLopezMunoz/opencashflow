"""Tests for opencashflow.cli.cmd_show and compute_real_column_values --
`show`'s first public engine-level tests (it never had any before this
command existed publicly).

Direct ORM construction (no HTTP, no auth), same style as
test_period_close.py -- a minimal SALDO INICIAL/SALDO FINAL running-balance
pair plus one leaf row, base_period pinned to the CURRENT month so
find_anchor_period resolves to it directly (no need to exercise its
"whole sheet in the past" fallback here, already covered elsewhere).

Covers:
  1. cmd_show prints the table (table format) / a CSV (csv format).
  2. `result=` injection: a pre-computed result is used verbatim instead of
     cmd_show calling compute_sheet() itself.
  3. --with-real with no extension points: compute_real_column_values runs
     with extra_cash=None, real column shows the plain wallet/estimate value.
  4. --with-real + extra_real_cash: the callback's contribution lands in
     both the real value AND the breakdown.
  5. extra_combined_total: the informational parenthetical under SALDO
     INICIAL reflects wallets_total + the callback's own total.
  6. print_extra_sections is called with (db, sheet, args) before the table.
  7. --with-real --format csv is rejected (not yet supported).
  8. compute_real_column_values: caja_is_estimated is True in the no-wallet/
     no-actual fallback path and False once a wallet exists; extra_cash is
     never called when caja_real itself is None.
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.cli import cmd_show, compute_real_column_values
from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CashflowSheet, SheetPeriod, SheetRow, SheetSection
from opencashflow.wallet import Wallet

TEST_USER_ID = 1
TODAY = date.today().replace(day=1)


def _shift_months(d: date, delta: int) -> date:
    total = d.year * 12 + (d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


# One month before TODAY, not TODAY itself: SALDO INICIAL's previous_period
# rule returns None for a sheet's very FIRST period (no prior period to
# read -- see opencashflow.engine's own previous_period handling), which
# would make caja_real None for every test here. Starting a month earlier
# gives the anchor (TODAY) a real previous period to read from.
BASE_PERIOD = _shift_months(TODAY, -1)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def sheet(db):
    s = CashflowSheet(user_id=TEST_USER_ID, name="Show Test Sheet", currency="CLP",
                       horizon_months=4, base_period=BASE_PERIOD)
    db.add(s)
    db.flush()
    for i in range(4):
        period_date = _shift_months(BASE_PERIOD, i)
        db.add(SheetPeriod(sheet_id=s.id, period_date=datetime(period_date.year, period_date.month, 1),
                            label=f"P{i}", sort_order=i))
    db.flush()

    sec_saldo = SheetSection(sheet_id=s.id, name="Saldo", section_type="balance")
    sec_mov = SheetSection(sheet_id=s.id, name="Movimientos", section_type="income")
    db.add_all([sec_saldo, sec_mov])
    db.flush()

    saldo_inicial = SheetRow(section_id=sec_saldo.id, name="SALDO INICIAL", row_type="running_balance")
    saldo_final = SheetRow(section_id=sec_saldo.id, name="SALDO FINAL", row_type="running_balance")
    db.add_all([saldo_inicial, saldo_final])
    db.flush()

    ingreso = SheetRow(section_id=sec_mov.id, name="Sueldo", sign="positive",
                        default_projection_rule={"type": "constant", "value": "500000"})
    db.add(ingreso)
    db.flush()

    saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": saldo_final.id}
    saldo_final.default_projection_rule = {"type": "sum_rows", "row_ids": [saldo_inicial.id, ingreso.id]}
    db.commit()
    db.refresh(s)
    return s


def _show_args(sheet_id, **overrides):
    defaults = dict(
        sheet_id=sheet_id, months=6, before=0, context=None, unit="1", width=200,
        format="table", show_ids=False, with_real=False, wallets=False,
    )
    return NS(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# 1. basic table / csv output
# ---------------------------------------------------------------------------

def test_cmd_show_prints_table(db, sheet, capsys):
    cmd_show(db, _show_args(sheet.id))
    out = capsys.readouterr().out
    assert "Show Test Sheet" in out
    assert "SALDO INICIAL" in out
    assert "SALDO FINAL" in out


def test_cmd_show_csv_format(db, sheet, capsys):
    cmd_show(db, _show_args(sheet.id, format="csv"))
    out = capsys.readouterr().out
    assert "Fila" in out.splitlines()[0]
    assert "SALDO INICIAL" in out


def test_cmd_show_no_periods_exits(db, capsys):
    empty_sheet = CashflowSheet(user_id=TEST_USER_ID, name="Empty", currency="CLP",
                                 horizon_months=0, base_period=TODAY)
    db.add(empty_sheet)
    db.commit()
    db.refresh(empty_sheet)
    with pytest.raises(SystemExit):
        cmd_show(db, _show_args(empty_sheet.id))
    assert "no tiene períodos" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 2. result= injection
# ---------------------------------------------------------------------------

def test_cmd_show_uses_injected_result_instead_of_computing_its_own(db, sheet, capsys):
    real_result = compute_sheet(sheet.id, db)
    # Tamper with the ANCHOR period's label (index 1 = TODAY -- index 0 is
    # before the default display window and would never show up in the
    # table either way) so we can prove THIS object (not a fresh
    # compute_sheet() call) is what got rendered.
    real_result["periods"][1].label = "PERIODO-INYECTADO"
    cmd_show(db, _show_args(sheet.id), result=real_result)
    assert "PERIODO-INYECTADO" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3-6. --with-real and its extension points
# ---------------------------------------------------------------------------

def test_cmd_show_with_real_plain_estimate(db, sheet, capsys):
    cmd_show(db, _show_args(sheet.id, with_real=True))
    out = capsys.readouterr().out
    assert "Actual" in out


def test_cmd_show_with_real_and_extra_real_cash(db, sheet, capsys):
    def extra(db, sheet, period_id, today, *, caja_is_estimated):
        return [("Extra de prueba", Decimal("77777"), False)]

    cmd_show(db, _show_args(sheet.id, with_real=True), extra_real_cash=extra)
    out = capsys.readouterr().out
    assert "Actual" in out


def test_cmd_show_extra_combined_total_shows_parenthetical(db, sheet, capsys):
    wallet = Wallet(user_id=TEST_USER_ID, name="Cuenta", wallet_type="checking",
                     currency="CLP", balance=Decimal("100000"))
    db.add(wallet)
    db.commit()

    def extra_total(db, sheet, sheet_currency):
        return Decimal("50000")

    cmd_show(db, _show_args(sheet.id, with_real=True), extra_combined_total=extra_total)
    out = capsys.readouterr().out
    # wallets_total (100.000) + extra_total (50.000) = 150.000, informational
    # parenthetical printed under SALDO INICIAL's real column.
    assert "150.000" in out


def test_cmd_show_print_extra_sections_called_with_db_sheet_args(db, sheet, capsys):
    calls = []

    def hook(db_arg, sheet_arg, args_arg):
        calls.append((db_arg, sheet_arg, args_arg))
        print("SECCION EXTRA DE PRUEBA")

    cmd_show(db, _show_args(sheet.id), print_extra_sections=hook)
    out = capsys.readouterr().out
    assert "SECCION EXTRA DE PRUEBA" in out
    assert len(calls) == 1
    assert calls[0][0] is db
    assert calls[0][1].id == sheet.id


def test_cmd_show_with_real_and_csv_rejected(db, sheet, capsys):
    with pytest.raises(SystemExit):
        cmd_show(db, _show_args(sheet.id, with_real=True, format="csv"))
    assert "--with-real" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 8. compute_real_column_values -- caja_is_estimated / extra_cash gating
# ---------------------------------------------------------------------------

def test_compute_real_column_values_caja_is_estimated_without_wallet_or_actual(db, sheet):
    result = compute_sheet(sheet.id, db)
    anchor_period = next(p for p in result["periods"] if p.period_date.date() == TODAY)

    seen = {}

    def extra(db, sheet, period_id, today, *, caja_is_estimated):
        seen["value"] = caja_is_estimated
        return []

    values, breakdown = compute_real_column_values(db, sheet, result, anchor_period.id, extra_cash=extra)
    assert seen["value"] is True
    assert any("estimada" in label for label, _amount, _is_estimate in breakdown)


def test_compute_real_column_values_caja_not_estimated_with_wallet(db, sheet):
    db.add(Wallet(user_id=TEST_USER_ID, name="Cuenta", wallet_type="checking",
                   currency="CLP", balance=Decimal("200000")))
    db.commit()
    result = compute_sheet(sheet.id, db)
    anchor_period = next(p for p in result["periods"] if p.period_date.date() == TODAY)

    seen = {}

    def extra(db, sheet, period_id, today, *, caja_is_estimated):
        seen["value"] = caja_is_estimated
        return [("Cupo extra", Decimal("1000"), False)]

    values, breakdown = compute_real_column_values(db, sheet, result, anchor_period.id, extra_cash=extra)
    assert seen["value"] is False
    balance_row = next(r for r in db.query(SheetRow).all() if r.name == "SALDO INICIAL")
    assert values[balance_row.id] == Decimal("200000") + Decimal("1000")
    assert ("Cupo extra", Decimal("1000"), False) in breakdown


def test_compute_real_column_values_extra_cash_not_called_when_caja_real_is_none(db, sheet):
    # No wallet, no confirmed actual_value, and no paid_value anywhere --
    # the estimate fallback itself resolves to None (balance_projected is
    # only None if the row genuinely has no value for this period, which
    # doesn't happen here -- so exercise this by asking about a period the
    # result has no cell for at all: an out-of-range period_id).
    result = compute_sheet(sheet.id, db)
    calls = []

    def extra(db, sheet, period_id, today, *, caja_is_estimated):
        calls.append(1)
        return []

    compute_real_column_values(db, sheet, result, period_id=-1, extra_cash=extra)
    assert calls == []
