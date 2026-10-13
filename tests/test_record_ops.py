"""Tests for opencashflow.record_ops -- the real layer's write/clear/undo/
history primitive (cli.py aliases these as _do_record_*), plus the CLI's
`record set` wrapper, whose inline write path used to skip the closed-period
guard entirely.

Direct ORM construction, same style as test_overrides.py.
"""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.cli import cmd_record_set
from opencashflow.models import Base, CashflowSheet, CellActualEntry, SheetCell, SheetPeriod, SheetRow, SheetSection
from opencashflow.record_ops import UNSET, clear_record, record_history, set_record, undo_record
from opencashflow.record_stack import EmptyRecordStackError

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
    s = CashflowSheet(user_id=TEST_USER_ID, name="Record Ops Test Sheet", currency="CLP",
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


def _period(sheet, label):
    return next(p for p in sheet.periods if p.label == label)


def _cell(db, row, period):
    return db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()


def _entries(db, row, period):
    cell = _cell(db, row, period)
    return [] if cell is None else db.query(CellActualEntry).filter(CellActualEntry.cell_id == cell.id).all()


# ---------------------------------------------------------------------------
# set_record
# ---------------------------------------------------------------------------

def test_set_record_writes_only_the_given_fields_and_logs_a_snapshot(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    first = set_record(db, row, p1, accrued_value=Decimal("500000"), created_by=TEST_USER_ID)
    assert first.previous == (None, None, None)
    assert first.current == (None, Decimal("500000"), None)

    second = set_record(db, row, p1, paid_value=Decimal("300000"), note="pago parcial", created_by=TEST_USER_ID)
    assert second.previous == (None, Decimal("500000"), None)
    assert second.current == (None, Decimal("500000"), Decimal("300000"))  # accrued untouched (UNSET)
    assert second.entry.note == "pago parcial"
    assert len(_entries(db, row, p1)) == 2


def test_set_record_explicit_none_clears_just_that_field(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    set_record(db, row, p1, accrued_value=Decimal("500000"), paid_value=Decimal("100"), created_by=TEST_USER_ID)
    result = set_record(db, row, p1, paid_value=None, created_by=TEST_USER_ID)
    assert result.current == (None, Decimal("500000"), None)


def test_set_record_with_nothing_to_write_raises_and_writes_nothing(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    with pytest.raises(ValueError):
        set_record(db, row, p1, created_by=TEST_USER_ID)
    assert _cell(db, row, p1) is None


def test_set_record_refuses_a_closed_period_and_writes_nothing(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    p1.is_closed = True
    db.commit()

    with pytest.raises(ValueError, match="closed period"):
        set_record(db, row, p1, actual_value=Decimal("1"), created_by=TEST_USER_ID)
    assert _cell(db, row, p1) is None


def test_unset_is_a_single_shared_sentinel():
    from opencashflow import record_ops
    assert record_ops.UNSET is UNSET


# ---------------------------------------------------------------------------
# record_history / undo_record / clear_record
# ---------------------------------------------------------------------------

def test_record_history_flags_the_live_entry(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    set_record(db, row, p1, actual_value=Decimal("10"), created_by=TEST_USER_ID)
    set_record(db, row, p1, actual_value=Decimal("20"), created_by=TEST_USER_ID)

    history = record_history(db, row, p1)
    assert [h.entry.actual_value for h in history] == [Decimal("10"), Decimal("20")]
    assert [h.is_current for h in history] == [False, True]


def test_record_history_on_an_untouched_cell_is_empty(db, sheet, row):
    assert record_history(db, row, _period(sheet, "Jan 2026")) == []


def test_undo_record_walks_back_one_step(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    set_record(db, row, p1, actual_value=Decimal("10"), created_by=TEST_USER_ID)
    set_record(db, row, p1, actual_value=Decimal("20"), created_by=TEST_USER_ID)

    result = undo_record(db, row, p1, created_by=TEST_USER_ID)
    assert result.reverted_from.actual_value == Decimal("20")
    assert result.reverted_to.actual_value == Decimal("10")
    assert _cell(db, row, p1).actual_value == Decimal("10")


def test_undo_record_with_nothing_recorded_raises_empty_record_stack_error(db, sheet, row):
    with pytest.raises(EmptyRecordStackError):
        undo_record(db, row, _period(sheet, "Jan 2026"), created_by=TEST_USER_ID)


def test_undo_record_past_the_bottom_of_the_stack_raises_empty_record_stack_error(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    set_record(db, row, p1, actual_value=Decimal("10"), created_by=TEST_USER_ID)
    undo_record(db, row, p1, created_by=TEST_USER_ID)
    with pytest.raises(EmptyRecordStackError):
        undo_record(db, row, p1, created_by=TEST_USER_ID)


def test_undo_record_refuses_a_closed_period(db, sheet, row):
    p1 = _period(sheet, "Jan 2026")
    set_record(db, row, p1, actual_value=Decimal("10"), created_by=TEST_USER_ID)
    p1.is_closed = True
    db.commit()
    with pytest.raises(ValueError, match="closed period"):
        undo_record(db, row, p1, created_by=TEST_USER_ID)


def test_clear_record_resets_cleared_periods_and_reports_untouched_ones(db, sheet, row):
    p1, p2 = _period(sheet, "Jan 2026"), _period(sheet, "Feb 2026")
    set_record(db, row, p1, actual_value=Decimal("10"), paid_value=Decimal("5"), created_by=TEST_USER_ID)

    results = clear_record(db, row, [p1, p2], created_by=TEST_USER_ID)
    assert [r.cleared for r in results] == [True, False]
    assert results[0].previous == (Decimal("10"), None, Decimal("5"))
    cell = _cell(db, row, p1)
    assert (cell.actual_value, cell.accrued_value, cell.paid_value) == (None, None, None)


def test_clear_record_refuses_if_any_period_is_closed(db, sheet, row):
    p1, p2 = _period(sheet, "Jan 2026"), _period(sheet, "Feb 2026")
    set_record(db, row, p1, actual_value=Decimal("10"), created_by=TEST_USER_ID)
    p2.is_closed = True
    db.commit()
    with pytest.raises(ValueError, match="closed period"):
        clear_record(db, row, [p1, p2], created_by=TEST_USER_ID)
    assert _cell(db, row, p1).actual_value == Decimal("10")


# ---------------------------------------------------------------------------
# The CLI's `record set` -- the path that used to skip the guard entirely.
# ---------------------------------------------------------------------------

def _record_set_args(sheet, row, **overrides):
    base = dict(sheet_id=sheet.id, row=str(row.id), period="2026-01", actual=None, accrued=None,
                paid=None, note=None, user_id=None, unit="1")
    base.update(overrides)
    return NS(**base)


def test_cmd_record_set_refuses_a_closed_period_and_writes_nothing(db, sheet, row, capsys):
    p1 = _period(sheet, "Jan 2026")
    p1.is_closed = True
    db.commit()

    with pytest.raises(SystemExit):
        cmd_record_set(db, _record_set_args(sheet, row, actual=999999.0))
    assert "closed period" in capsys.readouterr().err
    assert _cell(db, row, p1) is None


def test_cmd_record_set_still_writes_on_an_open_period(db, sheet, row, capsys):
    cmd_record_set(db, _record_set_args(sheet, row, accrued=500000.0, paid=300000.0))
    out = capsys.readouterr().out
    assert "[OK] Sueldo" in out
    cell = _cell(db, row, _period(sheet, "Jan 2026"))
    assert (cell.accrued_value, cell.paid_value) == (Decimal("500000"), Decimal("300000"))
