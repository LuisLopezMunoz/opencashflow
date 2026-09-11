"""Tests for opencashflow.overrides.set_override/clear_override -- the
shared primitive behind `override set`/`override clear` (cli.py aliases
these directly, see test_cli_override_closed_period.py for the closed-
period guard exercised through that alias) and reusable as-is by any other
consumer needing the same guarantees.

Direct ORM construction, same style as test_period_close.py.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import Base, CashflowSheet, CellOverride, SheetPeriod, SheetRow, SheetSection
from opencashflow.overrides import clear_override, set_override

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
    s = CashflowSheet(user_id=TEST_USER_ID, name="Overrides Test Sheet", currency="CLP",
                       horizon_months=2, base_period=datetime(2026, 1, 1))
    db.add(s)
    db.flush()
    db.add_all([
        SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 1, 1), label="Jan 2026", sort_order=0),
        SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 2, 1), label="Feb 2026", sort_order=1),
    ])
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def row(db, sheet):
    section = SheetSection(sheet_id=sheet.id, name="Movimientos", section_type="custom")
    db.add(section)
    db.flush()
    r = SheetRow(section_id=section.id, name="Sueldo", row_type="input",
                 default_projection_rule={"type": "constant", "value": 500_000})
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _period(db, sheet, label):
    return next(p for p in sheet.periods if p.label == label)


def _active_override(db, row_id, period_id):
    from opencashflow.models import SheetCell
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        return None
    return next((ov for ov in cell.overrides if ov.superseded_at is None), None)


def test_set_override_writes_a_manual_value(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    results = set_override(db, sheet.id, row, [p1], value=Decimal("999999"), created_by=TEST_USER_ID)

    assert len(results) == 1
    assert results[0].override.value == Decimal("999999")
    assert results[0].override.override_type == "manual_value"
    assert results[0].previous_override is None

    active = _active_override(db, row.id, p1.id)
    assert active.value == Decimal("999999")


def test_set_override_writes_a_manual_rule(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    rule = {"type": "constant", "value": 123}
    results = set_override(db, sheet.id, row, [p1], rule=rule, created_by=TEST_USER_ID)

    assert results[0].override.override_type == "manual_rule"
    assert results[0].override.custom_rule == rule
    assert results[0].override.value is None


def test_set_override_supersedes_the_previous_one(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    set_override(db, sheet.id, row, [p1], value=Decimal("100"), created_by=TEST_USER_ID)
    second = set_override(db, sheet.id, row, [p1], value=Decimal("200"), created_by=TEST_USER_ID)

    assert second[0].previous_override.value == Decimal("100")
    assert second[0].override.value == Decimal("200")
    # Only one active override left on the cell.
    active = _active_override(db, row.id, p1.id)
    assert active.value == Decimal("200")
    assert db.query(CellOverride).filter(CellOverride.cell_id == active.cell_id).count() == 2


def test_set_override_lock_captures_current_effective_value(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    results = set_override(db, sheet.id, row, [p1], lock=True, created_by=TEST_USER_ID)

    assert results[0].skipped is False
    assert results[0].override.override_type == "lock"
    assert results[0].override.value == Decimal("500000")  # the row's own constant rule


def test_set_override_lock_skips_a_cell_with_no_effective_value_yet(db, sheet):
    section = SheetSection(sheet_id=sheet.id, name="Sin regla", section_type="custom")
    db.add(section)
    db.flush()
    ruleless_row = SheetRow(section_id=section.id, name="Imprevistos", row_type="input")
    db.add(ruleless_row)
    db.commit()

    p1 = _period(db, sheet, "Jan 2026")
    results = set_override(db, sheet.id, ruleless_row, [p1], lock=True, created_by=TEST_USER_ID)

    assert results[0].skipped is True
    assert results[0].override is None
    assert _active_override(db, ruleless_row.id, p1.id) is None


def test_clear_override_reverts_to_the_rows_own_rule(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    set_override(db, sheet.id, row, [p1], value=Decimal("999999"), created_by=TEST_USER_ID)

    results = clear_override(db, sheet.id, row, [p1])

    assert results[0].previous_override.value == Decimal("999999")
    assert results[0].reverted_value == Decimal("500000")  # back to the constant rule
    assert _active_override(db, row.id, p1.id) is None


def test_clear_override_on_a_cell_with_nothing_active_is_a_reported_no_op(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    results = clear_override(db, sheet.id, row, [p1])

    assert results[0].previous_override is None
    assert results[0].reverted_value == Decimal("500000")


def test_set_override_raises_on_a_closed_period_and_writes_nothing(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    p1.is_closed = True
    db.commit()

    with pytest.raises(ValueError, match="closed period"):
        set_override(db, sheet.id, row, [p1], value=Decimal("1"), created_by=TEST_USER_ID)
    assert _active_override(db, row.id, p1.id) is None


def test_clear_override_raises_on_a_closed_period(db, sheet, row):
    p1 = _period(db, sheet, "Jan 2026")
    p1.is_closed = True
    db.commit()

    with pytest.raises(ValueError, match="closed period"):
        clear_override(db, sheet.id, row, [p1])
