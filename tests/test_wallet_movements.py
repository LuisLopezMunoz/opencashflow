"""Unit tests for opencashflow.wallet_movements: do_wallet_movement_add,
do_wallet_movement_undo -- direction derived from a row's effective sign,
additive on paid_value, undo reverses both sides, and its own refusals.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import Base, CashflowSheet, CellOverride, SheetCell, SheetPeriod, SheetRow, SheetSection
from opencashflow.wallet import Wallet, WalletMovement
from opencashflow.wallet_movements import do_wallet_movement_add, do_wallet_movement_undo

TEST_DB_URL = "sqlite:///:memory:"
TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def built(db):
    """SALDO INICIAL/SALDO FINAL pair, one income leaf (Sueldo, +) and one
    expense leaf (Arriendo, sign=negative), both feeding SALDO FINAL
    directly, plus one row with no sum_rows parent (Fila Huerfana) to prove
    rejection -- and one Wallet."""
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Wallet Movement Test Sheet", currency="CLP",
                           horizon_months=3, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    for i in range(3):
        db.add(SheetPeriod(sheet_id=sheet.id, period_date=datetime(2026, 1 + i, 1), label=f"P{i}", sort_order=i))
    db.flush()

    sec_saldo = SheetSection(sheet_id=sheet.id, name="Saldo", section_type="balance")
    sec_ingresos = SheetSection(sheet_id=sheet.id, name="Ingresos", section_type="income")
    sec_gastos = SheetSection(sheet_id=sheet.id, name="Gastos", section_type="expense")
    db.add_all([sec_saldo, sec_ingresos, sec_gastos])
    db.flush()

    saldo_inicial = SheetRow(section_id=sec_saldo.id, name="SALDO INICIAL", row_type="running_balance")
    sueldo = SheetRow(section_id=sec_ingresos.id, name="Sueldo", sign="positive",
                       default_projection_rule={"type": "constant", "value": 1_000_000})
    arriendo = SheetRow(section_id=sec_gastos.id, name="Arriendo", sign="negative",
                         default_projection_rule={"type": "constant", "value": 400_000})
    huerfana = SheetRow(section_id=sec_gastos.id, name="Fila Huerfana", sign="positive",
                         default_projection_rule={"type": "constant", "value": 1})
    db.add_all([saldo_inicial, sueldo, arriendo, huerfana])
    db.commit()

    saldo_final = SheetRow(section_id=sec_saldo.id, name="SALDO FINAL", row_type="running_balance",
                            default_projection_rule={"type": "sum_rows", "row_ids": [saldo_inicial.id, sueldo.id, arriendo.id]})
    db.add(saldo_final)
    db.commit()
    saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": saldo_final.id}
    db.commit()

    period1 = db.query(SheetPeriod).filter(SheetPeriod.sheet_id == sheet.id).order_by(SheetPeriod.sort_order).first()
    cell = SheetCell(row_id=saldo_inicial.id, period_id=period1.id)
    db.add(cell)
    db.flush()
    db.add(CellOverride(cell_id=cell.id, value=Decimal("0"), override_type="manual_value", created_by=TEST_USER_ID))
    db.commit()

    wallet = Wallet(user_id=TEST_USER_ID, name="Santander", wallet_type="bank", currency="CLP", balance=100_000.0)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    return {"sheet": sheet, "period1": period1, "wallet": wallet, "sueldo": sueldo, "arriendo": arriendo, "huerfana": huerfana}


# ---------------------------------------------------------------------------
# direction derived from the row, rejection with no aggregation path
# ---------------------------------------------------------------------------

def test_income_row_credits_the_wallet(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    result = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"),
                                     note="sueldo enero", created_by=TEST_USER_ID)
    assert result.wallet_balance_before == 100_000.0
    assert result.wallet_balance_after == 600_000.0
    assert result.cell_paid_before is None
    assert result.cell_paid_after == Decimal("500000")
    assert result.movement.amount == 500_000.0


def test_wallet_balance_and_movement_amount_are_real_decimals(db, built):
    # Regression: Wallet.balance/WalletMovement.amount used to be Float --
    # the VALUE could look right (Decimal == float compares fine for exact
    # values) while the TYPE silently wasn't Decimal at all. Assert the type
    # explicitly, not just the value.
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    result = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"),
                                     note="sueldo enero", created_by=TEST_USER_ID)
    assert isinstance(result.wallet_balance_before, Decimal)
    assert isinstance(result.wallet_balance_after, Decimal)
    assert isinstance(result.movement.amount, Decimal)
    assert isinstance(wallet.balance, Decimal)

    undo_result = do_wallet_movement_undo(db, result.movement, note="deshacer", created_by=TEST_USER_ID)
    assert isinstance(undo_result.wallet_balance_before, Decimal)
    assert isinstance(undo_result.wallet_balance_after, Decimal)
    assert isinstance(undo_result.reversal.amount, Decimal)


def test_expense_row_debits_the_wallet(db, built):
    wallet, arriendo, period = built["wallet"], built["arriendo"], built["period1"]
    result = do_wallet_movement_add(db, wallet, built["sheet"].id, arriendo, period, Decimal("400000"),
                                     note="arriendo enero", created_by=TEST_USER_ID)
    assert result.wallet_balance_after == 100_000.0 - 400_000.0
    assert result.cell_paid_after == Decimal("400000")
    assert result.movement.amount == -400_000.0


def test_negative_or_zero_amount_is_rejected(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    with pytest.raises(ValueError, match="positive"):
        do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("-1"), note=None, created_by=TEST_USER_ID)
    with pytest.raises(ValueError, match="positive"):
        do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("0"), note=None, created_by=TEST_USER_ID)


def test_row_with_no_aggregation_path_is_rejected(db, built):
    wallet, huerfana, period = built["wallet"], built["huerfana"], built["period1"]
    with pytest.raises(ValueError, match="sum_rows"):
        do_wallet_movement_add(db, wallet, built["sheet"].id, huerfana, period, Decimal("1"), note=None, created_by=TEST_USER_ID)
    db.refresh(wallet)
    assert wallet.balance == 100_000.0


# ---------------------------------------------------------------------------
# sheet_id/period/row/currency cross-checks -- do_wallet_movement_add used
# to take sheet_id as an independent parameter with no assertion that it
# actually matched the period or row passed alongside it, and no check that
# the wallet's currency matched the sheet's -- a caller bug (wrong id
# threaded through) or a cross-currency wallet used to silently write a
# WalletMovement with a wrong sheet_id, or mix amounts across currencies
# with no conversion, instead of raising.
# ---------------------------------------------------------------------------

def test_mismatched_sheet_id_and_period_is_rejected(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    with pytest.raises(ValueError, match="sheet_id/period"):
        do_wallet_movement_add(db, wallet, built["sheet"].id + 999, sueldo, period, Decimal("1"),
                                note=None, created_by=TEST_USER_ID)
    db.refresh(wallet)
    assert wallet.balance == 100_000.0


def test_mismatched_sheet_id_and_row_is_rejected(db):
    # A second, unrelated sheet with its own row -- passing the FIRST
    # sheet's id/period alongside the SECOND sheet's row must be rejected.
    other_sheet = CashflowSheet(user_id=TEST_USER_ID, name="Other Sheet", currency="CLP",
                                 horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(other_sheet)
    db.flush()
    other_section = SheetSection(sheet_id=other_sheet.id, name="Other Section", section_type="income")
    db.add(other_section)
    db.flush()
    other_row = SheetRow(section_id=other_section.id, name="Other Row", sign="positive")
    db.add(other_row)
    db.commit()

    built_db_sheet = CashflowSheet(user_id=TEST_USER_ID, name="Wallet Movement Test Sheet 2", currency="CLP",
                                    horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(built_db_sheet)
    db.flush()
    period = SheetPeriod(sheet_id=built_db_sheet.id, period_date=datetime(2026, 1, 1), label="P0", sort_order=0)
    db.add(period)
    db.commit()

    wallet = Wallet(user_id=TEST_USER_ID, name="Santander", wallet_type="bank", currency="CLP", balance=0)
    db.add(wallet)
    db.commit()

    with pytest.raises(ValueError, match="sheet_id/row"):
        do_wallet_movement_add(db, wallet, built_db_sheet.id, other_row, period, Decimal("1"),
                                note=None, created_by=TEST_USER_ID)


def test_mismatched_currency_is_rejected(db, built):
    sueldo, period = built["sueldo"], built["period1"]
    usd_wallet = Wallet(user_id=TEST_USER_ID, name="USD Account", wallet_type="bank",
                         currency="USD", balance=0)
    db.add(usd_wallet)
    db.commit()

    with pytest.raises(ValueError, match="currenc"):
        do_wallet_movement_add(db, usd_wallet, built["sheet"].id, sueldo, period, Decimal("1"),
                                note=None, created_by=TEST_USER_ID)
    db.refresh(usd_wallet)
    assert usd_wallet.balance == 0


# ---------------------------------------------------------------------------
# additive on the same cell, unlike an absolute set
# ---------------------------------------------------------------------------

def test_second_movement_same_cell_is_additive(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("300000"), note=None, created_by=TEST_USER_ID)
    result = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("200000"), note=None, created_by=TEST_USER_ID)
    assert result.cell_paid_before == Decimal("300000")
    assert result.cell_paid_after == Decimal("500000")
    db.refresh(wallet)
    assert wallet.balance == 100_000.0 + 300_000.0 + 200_000.0


# ---------------------------------------------------------------------------
# undo reverses both sides, and its own guards
# ---------------------------------------------------------------------------

def test_undo_reverses_wallet_and_cell(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    added = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"), note=None, created_by=TEST_USER_ID)

    result = do_wallet_movement_undo(db, added.movement, note="deshecho en test", created_by=TEST_USER_ID)
    db.refresh(wallet)
    cell = db.query(SheetCell).filter(SheetCell.row_id == sueldo.id, SheetCell.period_id == period.id).first()

    assert result.wallet_balance_before == 600_000.0
    assert result.wallet_balance_after == 100_000.0
    assert wallet.balance == 100_000.0
    assert cell.paid_value is None
    assert result.reversal.reverses_movement_id == added.movement.id
    assert result.reversal.amount == -500_000.0


def test_undo_twice_is_rejected(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    added = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"), note=None, created_by=TEST_USER_ID)
    do_wallet_movement_undo(db, added.movement, note=None, created_by=TEST_USER_ID)
    with pytest.raises(ValueError, match="already reversed"):
        do_wallet_movement_undo(db, added.movement, note=None, created_by=TEST_USER_ID)


def test_undoing_a_reversal_is_rejected(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    added = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"), note=None, created_by=TEST_USER_ID)
    undo_result = do_wallet_movement_undo(db, added.movement, note=None, created_by=TEST_USER_ID)
    with pytest.raises(ValueError, match="itself a reversal"):
        do_wallet_movement_undo(db, undo_result.reversal, note=None, created_by=TEST_USER_ID)


def test_reverses_movement_id_is_unique_at_the_db_level(db, built):
    """The actual backstop test_undo_twice_is_rejected's plain SELECT check
    can't cover: two WalletMovement rows can never share the same non-null
    reverses_movement_id, even bypassing do_wallet_movement_undo entirely.
    This is what protects against the real TOCTOU race -- two concurrent
    undo calls both passing the SELECT check before either commits -- that
    the SELECT check alone can't."""
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    added = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"),
                                    note=None, created_by=TEST_USER_ID)
    do_wallet_movement_undo(db, added.movement, note=None, created_by=TEST_USER_ID)

    # A second, independent reversal row for the SAME original movement --
    # exactly what two concurrent do_wallet_movement_undo calls would each
    # try to insert after both pass the (racy) SELECT check.
    db.add(WalletMovement(wallet_id=wallet.id, amount=Decimal("1"), sheet_id=built["sheet"].id,
                           row_id=sueldo.id, period_id=period.id, actual_entry_id=1,
                           reverses_movement_id=added.movement.id, created_by=TEST_USER_ID))
    with pytest.raises(Exception, match="UNIQUE"):
        db.commit()
    db.rollback()


def test_undo_refused_when_something_newer_sits_on_top(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    first = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("300000"), note=None, created_by=TEST_USER_ID)
    do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("200000"), note=None, created_by=TEST_USER_ID)
    with pytest.raises(ValueError, match="more recent"):
        do_wallet_movement_undo(db, first.movement, note=None, created_by=TEST_USER_ID)


def test_undo_and_add_reject_a_closed_period(db, built):
    wallet, sueldo, period = built["wallet"], built["sueldo"], built["period1"]
    added = do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("500000"), note=None, created_by=TEST_USER_ID)
    period.is_closed = True
    db.commit()

    with pytest.raises(ValueError, match="already-closed"):
        do_wallet_movement_add(db, wallet, built["sheet"].id, sueldo, period, Decimal("1"), note=None, created_by=TEST_USER_ID)
    with pytest.raises(ValueError, match="already-closed"):
        do_wallet_movement_undo(db, added.movement, note=None, created_by=TEST_USER_ID)
