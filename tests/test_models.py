"""Tests for opencashflow.models relationship configuration -- the parts
that aren't naturally exercised by exercising engine/period_close's own
behavior, like ordering guarantees and cascade rules.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import (
    Base,
    CashflowSheet,
    CellActualEntry,
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    SheetSection,
)
from opencashflow.wallet import Wallet, WalletMovement

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _cell(db) -> SheetCell:
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Order Test", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    section = SheetSection(sheet_id=sheet.id, name="S", section_type="custom")
    db.add(section)
    db.flush()
    row = SheetRow(section_id=section.id, name="A")
    db.add(row)
    db.flush()
    cell = SheetCell(row_id=row.id, period_id=1)
    db.add(cell)
    db.flush()
    return cell


# ---------------------------------------------------------------------------
# SheetCell.overrides / .actual_entries ordering -- created_at alone doesn't
# break a tie between two rows created in the exact same instant (plausible
# in a bulk import/seed loop). record_stack.py's push/pop algorithm depends
# on this relationship being in TRUE chronological (here: insertion) order
# to correctly tell which entry is "current" -- a tie resolved the wrong way
# by the database would silently corrupt that.
# ---------------------------------------------------------------------------

def test_overrides_break_same_instant_ties_by_insertion_order(db):
    cell = _cell(db)
    same_instant = datetime(2026, 1, 1, 12, 0, 0)
    first = CellOverride(cell_id=cell.id, value=1, override_type="manual_value",
                          created_by=TEST_USER_ID, created_at=same_instant)
    second = CellOverride(cell_id=cell.id, value=2, override_type="manual_value",
                           created_by=TEST_USER_ID, created_at=same_instant)
    db.add_all([first, second])
    db.commit()
    db.refresh(cell)

    assert [ov.id for ov in cell.overrides] == [first.id, second.id]


def test_actual_entries_break_same_instant_ties_by_insertion_order(db):
    cell = _cell(db)
    same_instant = datetime(2026, 1, 1, 12, 0, 0)
    first = CellActualEntry(cell_id=cell.id, accrued_value=1, paid_value=0,
                             created_by=TEST_USER_ID, created_at=same_instant)
    second = CellActualEntry(cell_id=cell.id, accrued_value=2, paid_value=0,
                              created_by=TEST_USER_ID, created_at=same_instant)
    db.add_all([first, second])
    db.commit()
    db.refresh(cell)

    assert [e.id for e in cell.actual_entries] == [first.id, second.id]


# ---------------------------------------------------------------------------
# Cascade hardening -- CellOverride/CellActualEntry are documented as
# append-only audit trails, and WalletMovement's own module docstring calls
# it one too. cascade="all, delete-orphan" on every parent above them used
# to contradict that: deleting a CashflowSheet, SheetSection, SheetRow,
# SheetPeriod, or SheetCell would hard-delete the whole audit trail beneath
# it with no soft-delete/archival alternative. No code path calls
# db.delete() on any of these today (this is preventive hardening, not a
# reproduced bug), so these tests assert the NEW behavior directly: deleting
# a parent object leaves its children as orphans in the session rather than
# cascading the delete down to them.
# ---------------------------------------------------------------------------

def _full_chain(db):
    """One sheet -> one section -> one row -> one period -> one cell -> one
    override + one actual_entry, plus a wallet with one movement pointing
    at that cell's actual_entry -- everything Phase 5 touches, in one tree."""
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Cascade Test", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    section = SheetSection(sheet_id=sheet.id, name="S", section_type="custom")
    db.add(section)
    db.flush()
    row = SheetRow(section_id=section.id, name="A")
    db.add(row)
    db.flush()
    period = SheetPeriod(sheet_id=sheet.id, period_date=datetime(2026, 1, 1), label="P0", sort_order=0)
    db.add(period)
    db.flush()
    cell = SheetCell(row_id=row.id, period_id=period.id)
    db.add(cell)
    db.flush()
    override = CellOverride(cell_id=cell.id, value=1, override_type="manual_value", created_by=TEST_USER_ID)
    entry = CellActualEntry(cell_id=cell.id, accrued_value=1, paid_value=0, created_by=TEST_USER_ID)
    db.add_all([override, entry])
    db.commit()
    return {"sheet": sheet, "section": section, "row": row, "period": period,
            "cell": cell, "override": override, "entry": entry}


def test_deleting_a_sheet_does_not_cascade_to_sections_or_periods(db):
    ids = _full_chain(db)
    section_id, period_id = ids["section"].id, ids["period"].id

    db.delete(ids["sheet"])
    db.commit()

    assert db.query(SheetSection).filter_by(id=section_id).first() is not None
    assert db.query(SheetPeriod).filter_by(id=period_id).first() is not None


def test_deleting_a_section_does_not_cascade_to_rows(db):
    ids = _full_chain(db)
    row_id = ids["row"].id

    db.delete(ids["section"])
    db.commit()

    assert db.query(SheetRow).filter_by(id=row_id).first() is not None


def test_deleting_a_row_does_not_cascade_to_cells(db):
    ids = _full_chain(db)
    cell_id = ids["cell"].id

    db.delete(ids["row"])
    db.commit()

    assert db.query(SheetCell).filter_by(id=cell_id).first() is not None


def test_deleting_a_period_does_not_cascade_to_cells(db):
    ids = _full_chain(db)
    cell_id = ids["cell"].id

    db.delete(ids["period"])
    db.commit()

    assert db.query(SheetCell).filter_by(id=cell_id).first() is not None


def test_deleting_a_cell_does_not_cascade_to_overrides_or_actual_entries(db):
    ids = _full_chain(db)
    override_id, entry_id = ids["override"].id, ids["entry"].id

    db.delete(ids["cell"])
    db.commit()

    assert db.query(CellOverride).filter_by(id=override_id).first() is not None
    assert db.query(CellActualEntry).filter_by(id=entry_id).first() is not None


def test_deleting_a_wallet_does_not_cascade_to_movements(db):
    wallet = Wallet(user_id=TEST_USER_ID, name="Santander", currency="CLP", balance=0)
    db.add(wallet)
    db.commit()
    movement = WalletMovement(wallet_id=wallet.id, amount=100, sheet_id=1, row_id=1,
                               period_id=1, actual_entry_id=1, created_by=TEST_USER_ID)
    db.add(movement)
    db.commit()
    movement_id = movement.id

    db.delete(wallet)
    db.commit()

    assert db.query(WalletMovement).filter_by(id=movement_id).first() is not None
