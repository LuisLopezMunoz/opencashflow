"""Unit tests for opencashflow.record_stack: replay_record_stack,
guard_periods_not_closed, pop_record_stack.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import Base, CashflowSheet, CellActualEntry, SheetCell, SheetPeriod, SheetRow, SheetSection
from opencashflow.record_stack import guard_periods_not_closed, pop_record_stack, replay_record_stack

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


def _cell(db) -> SheetCell:
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Record Stack Test Sheet", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    section = SheetSection(sheet_id=sheet.id, name="Section", section_type="custom")
    db.add(section)
    db.flush()
    row = SheetRow(section_id=section.id, name="Row", sign="positive")
    db.add(row)
    period = SheetPeriod(sheet_id=sheet.id, period_date=datetime(2026, 1, 1), label="Jan-26", sort_order=0)
    db.add(period)
    db.flush()
    cell = SheetCell(row_id=row.id, period_id=period.id)
    db.add(cell)
    db.commit()
    return cell


def _push(db, cell, paid):
    entry = CellActualEntry(cell_id=cell.id, paid_value=Decimal(str(paid)), created_by=TEST_USER_ID)
    cell.paid_value = Decimal(str(paid))
    db.add(entry)
    db.commit()
    return entry


# ---------------------------------------------------------------------------
# replay_record_stack
# ---------------------------------------------------------------------------

def test_replay_stack_plain_pushes():
    e1, e2 = CellActualEntry(note=None), CellActualEntry(note="a real note")
    assert replay_record_stack([e1, e2]) == [e1, e2]


def test_replay_stack_undo_pops_the_top():
    e1, e2 = CellActualEntry(note=None), CellActualEntry(note="[record undo] ")
    assert replay_record_stack([e1, e2]) == []


def test_replay_stack_repeated_undo_walks_backward_not_ping_pong():
    e1 = CellActualEntry(note=None)
    e2 = CellActualEntry(note=None)
    e3 = CellActualEntry(note=None)
    undo1 = CellActualEntry(note="[record undo] ")  # pops e3
    undo2 = CellActualEntry(note="[record undo] ")  # must pop e2, NOT redo e3
    assert replay_record_stack([e1, e2, e3, undo1]) == [e1, e2]
    assert replay_record_stack([e1, e2, e3, undo1, undo2]) == [e1]


def test_replay_stack_undo_on_empty_stack_is_a_noop():
    undo = CellActualEntry(note="[record undo] ")
    assert replay_record_stack([undo]) == []


# ---------------------------------------------------------------------------
# guard_periods_not_closed
# ---------------------------------------------------------------------------

def test_guard_passes_when_nothing_closed(db):
    cell = _cell(db)
    guard_periods_not_closed([cell.period], "do something")  # no raise


def test_guard_raises_naming_closed_periods(db):
    cell = _cell(db)
    cell.period.is_closed = True
    db.commit()

    with pytest.raises(ValueError, match="Jan-26"):
        guard_periods_not_closed([cell.period], "undo a record")


# ---------------------------------------------------------------------------
# pop_record_stack
# ---------------------------------------------------------------------------

def test_pop_reverts_to_the_previous_entry(db):
    cell = _cell(db)
    _push(db, cell, 100)
    e2 = _push(db, cell, 300)

    reverted_from, reverted_to, new_entry = pop_record_stack(db, cell, note="test undo", created_by=TEST_USER_ID)
    db.commit()

    assert reverted_from is e2
    assert reverted_to.paid_value == Decimal("100")
    assert cell.paid_value == Decimal("100")
    assert new_entry.note == "[record undo] test undo"


def test_pop_with_only_one_entry_reverts_to_none(db):
    cell = _cell(db)
    _push(db, cell, 100)

    _, reverted_to, _ = pop_record_stack(db, cell, note=None, created_by=TEST_USER_ID)
    db.commit()

    assert reverted_to is None
    assert cell.paid_value is None


def test_pop_on_empty_stack_raises_sentinel(db):
    cell = _cell(db)
    with pytest.raises(ValueError, match="__empty_stack__"):
        pop_record_stack(db, cell, note=None, created_by=TEST_USER_ID)


def test_pop_twice_walks_backward_not_ping_pong(db):
    cell = _cell(db)
    _push(db, cell, 100)
    _push(db, cell, 200)
    _push(db, cell, 300)

    pop_record_stack(db, cell, note=None, created_by=TEST_USER_ID)
    db.commit()
    assert cell.paid_value == Decimal("200")

    pop_record_stack(db, cell, note=None, created_by=TEST_USER_ID)
    db.commit()
    assert cell.paid_value == Decimal("100")
