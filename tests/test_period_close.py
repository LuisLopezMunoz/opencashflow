"""Unit tests for opencashflow.period_close.close_period().

Direct ORM construction (no HTTP, no auth -- this package doesn't know
either exists), same style as test_engine_rules.py.

Covers:
  1. Happy path: a mix of fully-settled and partially-paid rows closes
     correctly -- exact ending balance and exact rollover value, computed by
     hand in the test.
  2. Closing an already-closed period raises ValueError and writes nothing.
  3. Closing out of order (an earlier period still open) is rejected and
     names the open period.
  4. dry_run=True yields the identical report as a real run but persists
     nothing.
  5. A row with a nonzero projection but no accrued_value ever recorded is,
     by default, a warning (not a rollover) -- and IS rolled forward when
     assume_unrecorded_as_pending=True.
  6. A carry_forward row is skipped in the rollover-write step (no override
     written for it) -- a follow-up compute_sheet() shows it already
     picking up the carried amount on its own.

The HTTP-wrapping behavior (200/400 status codes) is a consuming app's own
concern and is NOT ported here -- close_period() itself never touches any
web-framework exception type.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CashflowSheet, CellActualEntry, CellOverride, SheetCell, SheetPeriod, SheetRow, SheetSection
from opencashflow.period_close import RolloverEntry, close_period

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


def _build_sheet(db, *, months, leaf_rows, base_period=datetime(2026, 1, 1)):
    """A minimal throwaway sheet: a SALDO INICIAL / SALDO FINAL running-
    balance pair (SALDO INICIAL = previous_period(row_id=SALDO FINAL),
    SALDO FINAL = sum_rows([SALDO INICIAL, *leaf_rows])), plus one leaf row
    per entry in `leaf_rows` (each: {"name", "sign" (default "positive"),
    "rule"}). Two passes, since sum_rows needs real row ids."""
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Close Test Sheet", currency="CLP",
                           horizon_months=months, base_period=base_period)
    db.add(sheet)
    db.flush()
    for i in range(months):
        month = base_period.month + i
        year = base_period.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        db.add(SheetPeriod(sheet_id=sheet.id, period_date=datetime(year, month, 1),
                            label=f"P{i}", sort_order=i))
    db.flush()

    sec_saldo = SheetSection(sheet_id=sheet.id, name="Saldo", section_type="balance")
    sec_mov = SheetSection(sheet_id=sheet.id, name="Movimientos", section_type="custom")
    db.add_all([sec_saldo, sec_mov])
    db.flush()

    saldo_inicial = SheetRow(section_id=sec_saldo.id, name="SALDO INICIAL", row_type="running_balance")
    saldo_final = SheetRow(section_id=sec_saldo.id, name="SALDO FINAL", row_type="running_balance")
    db.add_all([saldo_inicial, saldo_final])
    db.flush()

    row_ids = {}
    for spec in leaf_rows:
        row = SheetRow(section_id=sec_mov.id, name=spec["name"], sign=spec.get("sign", "positive"),
                        default_projection_rule=spec["rule"])
        db.add(row)
        db.flush()
        row_ids[spec["name"]] = row.id

    saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": saldo_final.id}
    saldo_final.default_projection_rule = {"type": "sum_rows", "row_ids": [saldo_inicial.id] + list(row_ids.values())}
    db.commit()

    periods = sorted(sheet.periods, key=lambda p: p.sort_order)
    return {
        "sheet_id": sheet.id, "periods": periods,
        "saldo_inicial_id": saldo_inicial.id, "saldo_final_id": saldo_final.id, "row_ids": row_ids,
    }


def _write_actual(db, row_id, period_id, *, actual_value=None, accrued_value=None, paid_value=None):
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    if actual_value is not None:
        cell.actual_value = Decimal(str(actual_value))
    if accrued_value is not None:
        cell.accrued_value = Decimal(str(accrued_value))
    if paid_value is not None:
        cell.paid_value = Decimal(str(paid_value))
    db.add(CellActualEntry(cell_id=cell.id, actual_value=cell.actual_value, accrued_value=cell.accrued_value,
                            paid_value=cell.paid_value, created_by=TEST_USER_ID))
    db.commit()


def _write_override(db, row_id, period_id, value):
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    db.add(CellOverride(cell_id=cell.id, value=Decimal(str(value)), override_type="manual_value", created_by=TEST_USER_ID))
    db.commit()


def _seal_history_period(db, saldo_final_row_id, period_id, value):
    """Mark a period as already-closed history -- write SALDO FINAL directly
    and flip is_closed, WITHOUT going through close_period(). Guard 1's
    contract explicitly allows a period to reach is_closed=True by some
    other means (e.g. a historical backfill)."""
    _write_override(db, saldo_final_row_id, period_id, value)
    period = db.query(SheetPeriod).filter(SheetPeriod.id == period_id).first()
    period.is_closed = True
    db.commit()


def _active_override_value(db, row_id, period_id):
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        return None
    ov = db.query(CellOverride).filter(CellOverride.cell_id == cell.id, CellOverride.superseded_at.is_(None)).first()
    return ov.value if ov else None


def _count_overrides(db):
    return db.query(CellOverride).count()


def _period(db, period_id):
    return db.query(SheetPeriod).filter(SheetPeriod.id == period_id).first()


def _sheet(db, sheet_id):
    return db.query(CashflowSheet).filter_by(id=sheet_id).first()


# ---------------------------------------------------------------------------
# 1. Happy path -- exact numbers, computed by hand
# ---------------------------------------------------------------------------

def test_happy_path_exact_saldo_final_and_rollover(db):
    ids = _build_sheet(
        db, months=3,
        leaf_rows=[
            {"name": "Sueldo", "sign": "positive", "rule": {"type": "constant", "value": 1_000_000}},
            {"name": "Arriendo", "sign": "negative", "rule": {"type": "constant", "value": 300_000}},
            {"name": "Servicios", "sign": "negative", "rule": {"type": "constant", "value": 100_000}},
        ],
    )
    p0, p1, p2 = (p.id for p in ids["periods"])

    _seal_history_period(db, ids["saldo_final_id"], p0, 500_000)

    _write_actual(db, ids["row_ids"]["Sueldo"], p1, actual_value=1_000_000, accrued_value=1_000_000, paid_value=1_000_000)
    _write_actual(db, ids["row_ids"]["Arriendo"], p1, actual_value=300_000, accrued_value=300_000, paid_value=300_000)
    _write_actual(db, ids["row_ids"]["Servicios"], p1, actual_value=120_000, accrued_value=120_000, paid_value=80_000)

    report = close_period(db, _sheet(db, ids["sheet_id"]), _period(db, p1), TEST_USER_ID)

    # real_net_flow = +1,000,000 (Sueldo) - 300,000 (Arriendo) - 80,000 (Servicios paid) = 620,000
    assert report.saldo_inicial_value == Decimal("500000")
    assert report.real_net_flow == Decimal("620000")
    assert report.saldo_final_value == Decimal("1120000")
    assert report.warnings == []
    assert report.rollovers == [RolloverEntry("Servicios", Decimal("40000"), "override written")]

    assert _active_override_value(db, ids["saldo_final_id"], p1) == Decimal("1120000")
    assert _active_override_value(db, ids["row_ids"]["Servicios"], p2) == Decimal("140000")
    assert _period(db, p1).is_closed is True


# ---------------------------------------------------------------------------
# 2. Closing an already-closed period is a no-op error
# ---------------------------------------------------------------------------

def test_close_already_closed_period_raises_and_writes_nothing(db):
    ids = _build_sheet(db, months=2, leaf_rows=[{"name": "Gasto", "sign": "negative", "rule": {"type": "constant", "value": 10_000}}])
    (p0,) = (p.id for p in ids["periods"][:1])
    _seal_history_period(db, ids["saldo_final_id"], p0, 100_000)

    before = _count_overrides(db)
    with pytest.raises(ValueError, match="already closed"):
        close_period(db, _sheet(db, ids["sheet_id"]), _period(db, p0), TEST_USER_ID)
    assert _count_overrides(db) == before


# ---------------------------------------------------------------------------
# 3. Closing out of order is rejected
# ---------------------------------------------------------------------------

def test_close_out_of_order_is_rejected(db):
    ids = _build_sheet(db, months=3, leaf_rows=[{"name": "Gasto", "sign": "negative", "rule": {"type": "constant", "value": 10_000}}])
    p0, p1, p2 = (p.id for p in ids["periods"])
    # p0 is left OPEN on purpose -- nothing seals it.

    with pytest.raises(ValueError) as excinfo:
        close_period(db, _sheet(db, ids["sheet_id"]), _period(db, p1), TEST_USER_ID)
    assert ids["periods"][0].label in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. dry_run persists nothing but reports exactly what a real run would
# ---------------------------------------------------------------------------

def test_dry_run_matches_real_run_but_persists_nothing(db):
    ids = _build_sheet(db, months=2, leaf_rows=[{"name": "Sueldo", "sign": "positive", "rule": {"type": "constant", "value": 500_000}}])
    p0, p1 = (p.id for p in ids["periods"])
    _seal_history_period(db, ids["saldo_final_id"], p0, 200_000)
    _write_actual(db, ids["row_ids"]["Sueldo"], p1, accrued_value=500_000, paid_value=500_000)

    before_overrides = _count_overrides(db)
    dry_report = close_period(db, _sheet(db, ids["sheet_id"]), _period(db, p1), TEST_USER_ID, dry_run=True)

    assert _count_overrides(db) == before_overrides
    assert _period(db, p1).is_closed is False
    assert _active_override_value(db, ids["saldo_final_id"], p1) is None

    real_report = close_period(db, _sheet(db, ids["sheet_id"]), _period(db, p1), TEST_USER_ID, dry_run=False)

    assert dry_report.saldo_inicial_value == real_report.saldo_inicial_value == Decimal("200000")
    assert dry_report.real_net_flow == real_report.real_net_flow == Decimal("500000")
    assert dry_report.saldo_final_value == real_report.saldo_final_value == Decimal("700000")
    assert dry_report.rollovers == real_report.rollovers == []
    assert dry_report.warnings == real_report.warnings == []
    assert dry_report.dry_run is True
    assert real_report.dry_run is False

    assert _active_override_value(db, ids["saldo_final_id"], p1) == Decimal("700000")
    assert _period(db, p1).is_closed is True


# ---------------------------------------------------------------------------
# 5. Unrecorded-but-projected row -- warning by default, pending when
# assume_unrecorded_as_pending=True
# ---------------------------------------------------------------------------

def test_unrecorded_row_is_warning_by_default_and_pending_when_assumed(db):
    ids = _build_sheet(db, months=4, leaf_rows=[{"name": "GastoFantasma", "sign": "negative", "rule": {"type": "constant", "value": 70_000}}])
    p0, p1, p2, p3 = (p.id for p in ids["periods"])
    gasto_id = ids["row_ids"]["GastoFantasma"]
    _seal_history_period(db, ids["saldo_final_id"], p0, 1_000_000)

    sheet = _sheet(db, ids["sheet_id"])

    report1 = close_period(db, sheet, _period(db, p1), TEST_USER_ID)
    assert report1.warnings == ["GastoFantasma"]
    assert report1.rollovers == []
    assert report1.real_net_flow == Decimal("0")
    assert report1.saldo_final_value == report1.saldo_inicial_value == Decimal("1000000")
    cell_p1 = db.query(SheetCell).filter(SheetCell.row_id == gasto_id, SheetCell.period_id == p1).first()
    assert cell_p1 is None or cell_p1.accrued_value is None

    report2 = close_period(db, sheet, _period(db, p2), TEST_USER_ID, assume_unrecorded_as_pending=True)
    assert report2.warnings == []
    assert len(report2.rollovers) == 1
    assert report2.rollovers[0].row_name == "GastoFantasma"
    assert report2.rollovers[0].pending_amount == Decimal("70000")

    cell_p2 = db.query(SheetCell).filter(SheetCell.row_id == gasto_id, SheetCell.period_id == p2).first()
    assert cell_p2.accrued_value == Decimal("70000")
    assert cell_p2.paid_value == Decimal("0")
    assert _active_override_value(db, gasto_id, p3) == Decimal("140000")


# ---------------------------------------------------------------------------
# 6. carry_forward rows self-correct -- no override written for them
# ---------------------------------------------------------------------------

def test_carry_forward_row_skipped_in_rollover_write(db):
    ids = _build_sheet(db, months=2, leaf_rows=[{
        "name": "Suscripcion", "sign": "negative",
        "rule": {"type": "carry_forward", "base_rule": {"type": "constant", "value": 20_000}},
    }])
    p0, p1 = (p.id for p in ids["periods"])
    sus_id = ids["row_ids"]["Suscripcion"]

    # p0 has no previous period, so give SALDO INICIAL an explicit override
    # instead of sealing history through SALDO FINAL.
    _write_override(db, ids["saldo_inicial_id"], p0, 200_000)
    _write_actual(db, sus_id, p0, accrued_value=20_000, paid_value=5_000)

    report = close_period(db, _sheet(db, ids["sheet_id"]), _period(db, p0), TEST_USER_ID)

    assert report.saldo_inicial_value == Decimal("200000")
    assert report.real_net_flow == Decimal("-5000")
    assert report.saldo_final_value == Decimal("195000")
    assert report.rollovers == [RolloverEntry("Suscripcion", Decimal("15000"), "auto (carry_forward rule)")]

    assert _active_override_value(db, sus_id, p1) is None

    result = compute_sheet(ids["sheet_id"], db)
    cell = next(
        cr for section in result["sections"] for row_data in section["rows"]
        if row_data["row"].id == sus_id for cr in row_data["cells"] if cr.period_id == p1
    )
    assert cell.projected_value == Decimal("35000")
    assert cell.error is None
