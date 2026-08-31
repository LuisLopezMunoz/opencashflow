"""Direct unit tests for opencashflow.engine.compute_sheet().

Build sheets directly against an ORM session and call compute_sheet()
in-process (no HTTP, no auth — this package doesn't know either exists).
They cover the rules added to unlock a real running balance:

  1. previous_period with an explicit row_id reads ANOTHER row.
  2. That cross-row, cross-period reference is NOT flagged as a cycle.
  3. sum_rows negates dependencies whose row has sign="negative".
  4. sum_rows with no sign set stays purely additive (backward compatibility).
  5. percent_of_row computes correctly and does participate in cycle detection.
  6. An unsupported rule type sets error="unsupported_rule:<type>" instead of
     silently returning an empty cell.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CashflowSheet, CellOverride, SheetCell, SheetPeriod, SheetRow, SheetSection

TEST_DB_URL = "sqlite:///:memory:"

# Ownership is an opaque id as far as this package is concerned — no User
# model exists here, so tests just pick a fixed id.
TEST_USER_ID = 1


@pytest.fixture()
def db():
    """A fresh in-memory SQLite session per test — no HTTP, no fixtures shared."""
    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _make_sheet(db, user_id, months=3) -> CashflowSheet:
    sheet = CashflowSheet(
        user_id=user_id,
        name="Rules Test Sheet",
        currency="CLP",
        horizon_months=months,
        base_period=datetime(2026, 1, 1),
    )
    db.add(sheet)
    db.flush()
    for i in range(months):
        db.add(
            SheetPeriod(
                sheet_id=sheet.id,
                period_date=datetime(2026, 1 + i, 1),
                label=f"P{i}",
                sort_order=i,
            )
        )
    db.flush()
    return sheet


def _make_section(db, sheet) -> SheetSection:
    section = SheetSection(sheet_id=sheet.id, name="Balance", section_type="balance")
    db.add(section)
    db.flush()
    return section


def _get_periods(db, sheet_id):
    return (
        db.query(SheetPeriod)
        .filter(SheetPeriod.sheet_id == sheet_id)
        .order_by(SheetPeriod.sort_order)
        .all()
    )


def _cells_for(matrix, row_id):
    for section in matrix["sections"]:
        for row_data in section["rows"]:
            if row_data["row"].id == row_id:
                return row_data["cells"]
    raise AssertionError(f"row {row_id} not found in matrix")


# ---------------------------------------------------------------------------
# 1 & 2: cross-row previous_period builds a running balance, without
#         tripping the cycle detector.
# ---------------------------------------------------------------------------

def test_running_balance_chain_via_cross_row_previous_period(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=3)
    section = _make_section(db, sheet)

    saldo_inicial = SheetRow(section_id=section.id, name="Saldo Inicial", row_type="balance", sort_order=0)
    flujo_neto = SheetRow(section_id=section.id, name="Flujo Neto", row_type="formula", sort_order=1,
                           default_projection_rule={"type": "constant", "value": 100})
    saldo_final = SheetRow(section_id=section.id, name="Saldo Final", row_type="running_balance", sort_order=2)
    db.add_all([saldo_inicial, flujo_neto, saldo_final])
    db.flush()

    saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": saldo_final.id}
    saldo_final.default_projection_rule = {
        "type": "sum_rows",
        "row_ids": [saldo_inicial.id, flujo_neto.id],
    }
    db.commit()

    periods = _get_periods(db, sheet.id)

    # Seed the opening balance of period 0 with a manual override (there is
    # no "previous" period to read from at t=0).
    cell0 = SheetCell(row_id=saldo_inicial.id, period_id=periods[0].id)
    db.add(cell0)
    db.flush()
    db.add(CellOverride(cell_id=cell0.id, value=Decimal("1000"), override_type="manual_value",
                         created_by=TEST_USER_ID))
    db.commit()

    matrix = compute_sheet(sheet.id, db)

    si_cells = _cells_for(matrix, saldo_inicial.id)
    sf_cells = _cells_for(matrix, saldo_final.id)

    assert [c.error for c in si_cells] == [None, None, None]
    assert [c.error for c in sf_cells] == [None, None, None]

    # Saldo Inicial: 1000 (manual), then chained from Saldo Final[t-1]
    assert si_cells[0].projected_value == Decimal("1000")
    assert si_cells[1].projected_value == Decimal("1100")
    assert si_cells[2].projected_value == Decimal("1200")

    # Saldo Final[t] = Saldo Inicial[t] + Flujo Neto[t] (100 every period)
    assert sf_cells[0].projected_value == Decimal("1100")
    assert sf_cells[1].projected_value == Decimal("1200")
    assert sf_cells[2].projected_value == Decimal("1300")

    # Cross-check the invariant: Saldo Final[t] == Saldo Inicial[t+1]
    assert sf_cells[0].projected_value == si_cells[1].projected_value
    assert sf_cells[1].projected_value == si_cells[2].projected_value


# ---------------------------------------------------------------------------
# 3: sum_rows negates rows whose sign="negative"
# ---------------------------------------------------------------------------

def test_sum_rows_negates_negative_sign_rows(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    income = SheetRow(section_id=section.id, name="Total Ingresos", sort_order=0, sign="positive",
                       default_projection_rule={"type": "constant", "value": 1000})
    expense = SheetRow(section_id=section.id, name="Total Egresos", sort_order=1, sign="negative",
                        default_projection_rule={"type": "constant", "value": 400})
    net = SheetRow(section_id=section.id, name="Flujo Neto", sort_order=2)
    db.add_all([income, expense, net])
    db.flush()
    net.default_projection_rule = {"type": "sum_rows", "row_ids": [income.id, expense.id]}
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    net_cells = _cells_for(matrix, net.id)
    assert net_cells[0].projected_value == Decimal("600")  # 1000 - 400
    assert net_cells[0].error is None


# ---------------------------------------------------------------------------
# 4: sum_rows with default sign ("positive") is purely additive — backward
#    compatible with sheets/tests that never set `sign`.
# ---------------------------------------------------------------------------

def test_sum_rows_default_sign_is_purely_additive(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    a = SheetRow(section_id=section.id, name="A", sort_order=0,
                 default_projection_rule={"type": "constant", "value": 10})
    b = SheetRow(section_id=section.id, name="B", sort_order=1,
                 default_projection_rule={"type": "constant", "value": 5})
    total = SheetRow(section_id=section.id, name="Total", sort_order=2)
    db.add_all([a, b, total])
    db.flush()
    total.default_projection_rule = {"type": "sum_rows", "row_ids": [a.id, b.id]}
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    total_cells = _cells_for(matrix, total.id)
    assert total_cells[0].projected_value == Decimal("15")


# ---------------------------------------------------------------------------
# 5: percent_of_row computes the right value AND participates in cycle
#    detection (unlike previous_period).
# ---------------------------------------------------------------------------

def test_percent_of_row_value(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    base = SheetRow(section_id=section.id, name="Total Ingresos", sort_order=0,
                     default_projection_rule={"type": "constant", "value": 2000})
    pct = SheetRow(section_id=section.id, name="Ahorro programado", sort_order=1)
    db.add_all([base, pct])
    db.flush()
    pct.default_projection_rule = {"type": "percent_of_row", "row_id": base.id, "percent": 10}
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    pct_cells = _cells_for(matrix, pct.id)
    assert pct_cells[0].projected_value == Decimal("200")
    assert pct_cells[0].error is None


def test_percent_of_row_cycle_is_detected(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    a = SheetRow(section_id=section.id, name="A", sort_order=0)
    b = SheetRow(section_id=section.id, name="B", sort_order=1)
    db.add_all([a, b])
    db.flush()
    a.default_projection_rule = {"type": "percent_of_row", "row_id": b.id, "percent": 10}
    b.default_projection_rule = {"type": "percent_of_row", "row_id": a.id, "percent": 10}
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    for row_id in (a.id, b.id):
        for cell in _cells_for(matrix, row_id):
            assert cell.error == "cycle_detected"
            assert cell.projected_value is None


# ---------------------------------------------------------------------------
# 6: unsupported rule type surfaces an error instead of a silent empty cell.
# ---------------------------------------------------------------------------

def test_unsupported_rule_sets_error(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    row = SheetRow(
        section_id=section.id,
        name="Rolling Average Row",
        sort_order=0,
        default_projection_rule={"type": "rolling_average", "n": 3},
    )
    db.add(row)
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    cells = _cells_for(matrix, row.id)
    assert cells[0].projected_value is None
    assert cells[0].error == "unsupported_rule:rolling_average"


# ---------------------------------------------------------------------------
# 7: actual_value/accrued_value/paid_value already stored on a cell pass
#    through, and pending_value/variance are derived from them.
# ---------------------------------------------------------------------------

def test_actual_value_and_variance_pass_through(db):
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    row = SheetRow(section_id=section.id, name="Vivienda", sort_order=0,
                    default_projection_rule={"type": "constant", "value": 400})
    db.add(row)
    db.commit()
    period = _get_periods(db, sheet.id)[0]

    cell = SheetCell(row_id=row.id, period_id=period.id,
                      actual_value=Decimal("435.80"),
                      accrued_value=Decimal("500"), paid_value=Decimal("300"))
    db.add(cell)
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    result = _cells_for(matrix, row.id)[0]
    assert result.projected_value == Decimal("400")
    assert result.actual_value == Decimal("435.80")
    assert result.variance == Decimal("35.80")
    assert result.accrued_value == Decimal("500")
    assert result.paid_value == Decimal("300")
    assert result.pending_value == Decimal("200")


def test_actual_value_absent_leaves_variance_none(db):
    """A cell with no real data recorded (the common case before a ledger
    bridge exists) must not error or fabricate a variance."""
    sheet = _make_sheet(db, TEST_USER_ID, months=1)
    section = _make_section(db, sheet)

    row = SheetRow(section_id=section.id, name="Sin datos reales", sort_order=0,
                    default_projection_rule={"type": "constant", "value": 100})
    db.add(row)
    db.commit()

    matrix = compute_sheet(sheet.id, db)
    result = _cells_for(matrix, row.id)[0]
    assert result.projected_value == Decimal("100")
    assert result.actual_value is None
    assert result.variance is None
    assert result.pending_value is None
