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
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import (
    Base,
    CashflowSheet,
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    SheetSection,
)
from opencashflow.schemas import (
    CellActualOut,
    CellActualUpdate,
    CellComputedOut,
    CellOverrideCreate,
    CellOverrideOut,
    MatrixOut,
    MatrixRowOut,
    MatrixSectionOut,
    PeriodOut,
    PeriodOutSimple,
    PeriodsBackfillCreate,
    RowCreate,
    RowOut,
    RowUpdate,
    SectionCreate,
    SectionOut,
    SectionWithRowsOut,
    SheetCreate,
    SheetDetailOut,
    SheetOut,
)

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
# PeriodOut / PeriodOutSimple -- the bug (and its fix)
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


def test_period_out_simple_matches_period_out_on_is_current(db):
    sheet = _sheet(db)
    today = datetime.utcnow()
    period = _period(db, sheet, datetime(today.year, today.month, 1))

    assert PeriodOut.model_validate(period).is_current == PeriodOutSimple.model_validate(period).is_current is True


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
# CellActualOut / CellComputedOut -- synthesized views spanning more than
# one ORM table (SheetCell + CellActualEntry, or the engine's own in-memory
# CellResult); no single ORM instance maps onto either 1:1, so these are
# exercised via plain keyword construction instead of model_validate.
# ---------------------------------------------------------------------------

def test_cell_actual_out_constructs_from_keyword_args():
    out = CellActualOut(row_id=1, period_id=1, actual_value=None, accrued_value=Decimal("100"),
                         paid_value=Decimal("40"), pending_value=Decimal("60"),
                         updated_by=TEST_USER_ID, updated_at=datetime(2026, 1, 1))
    assert out.pending_value == Decimal("60")


def test_cell_computed_out_constructs_from_keyword_args():
    out = CellComputedOut(row_id=1, period_id=1, projected_value=Decimal("100"), actual_value=None,
                           accrued_value=None, paid_value=None, pending_value=None, variance=None,
                           effective_source="rule")
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
        periods=[PeriodOutSimple.model_validate(period)],
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
    matrix = MatrixOut(sheet=SheetOut.model_validate(sheet), periods=[PeriodOutSimple.model_validate(period)],
                        sections=[matrix_section])

    assert matrix.sections[0].rows[0].cells[0].projected_value == Decimal("100")
