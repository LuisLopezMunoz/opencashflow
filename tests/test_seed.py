"""Tests for opencashflow.seed -- previously seed.py's own private
_override() never superseded a pre-existing active override before writing
a new one (unlike the supersede-then-insert pattern used everywhere else in
this codebase), so calling it twice on the same cell would silently leave
TWO simultaneously "active" (superseded_at IS NULL) overrides, violating
CellOverride's own documented invariant. It never actually happened in
seed_sheet() itself (every call site there targets a distinct cell), so
this is a direct unit test of _override() in isolation, not a seed_sheet()
regression.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import Base, CashflowSheet, CellOverride, SheetRow, SheetSection
from opencashflow.seed import _override

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_override_called_twice_leaves_only_one_active_override(db):
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Seed Override Test", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    section = SheetSection(sheet_id=sheet.id, name="S", section_type="custom")
    db.add(section)
    db.flush()
    row = SheetRow(section_id=section.id, name="A")
    db.add(row)
    db.commit()

    _override(db, TEST_USER_ID, row.id, period_id=1, value=100)
    db.commit()
    _override(db, TEST_USER_ID, row.id, period_id=1, value=200)
    db.commit()

    active = db.query(CellOverride).filter(CellOverride.superseded_at.is_(None)).all()
    assert len(active) == 1
    assert active[0].value == 200
