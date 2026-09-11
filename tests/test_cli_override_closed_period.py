"""_do_set_override/_do_clear_override (opencashflow.cli) must refuse to
write on a closed period, same as _do_record_undo/_do_record_clear already
do -- a closed period's real values are meant to stay frozen (see
opencashflow.period_close.close_period), and an unguarded override write was
a real gap: a plain `override set --period <closed>` used to silently
overwrite the exact value close_period had just frozen.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import date

from opencashflow.cli import _do_clear_override, _do_set_override
from opencashflow.models import Base, CellOverride, SheetRow
from opencashflow.seed import seed_sheet

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seeded_sheet(db):
    sheet = seed_sheet(db, user_id=TEST_USER_ID, months=3, base_period=date(2026, 1, 1))
    db.commit()
    row = db.query(SheetRow).filter(SheetRow.name == "Sueldo líquido").first()
    period = sorted(sheet.periods, key=lambda p: p.sort_order)[0]
    return sheet, row, period


def _count_overrides(db):
    return db.query(CellOverride).count()


def test_do_set_override_raises_on_closed_period_and_writes_nothing(db):
    sheet, row, period = _seeded_sheet(db)
    period.is_closed = True
    db.commit()
    overrides_before = _count_overrides(db)

    with pytest.raises(ValueError, match="closed period"):
        _do_set_override(
            db, sheet.id, row, [period], value=Decimal("999999"), rule=None, lock=False,
            note=None, created_by=TEST_USER_ID,
        )

    assert _count_overrides(db) == overrides_before


def test_do_clear_override_raises_on_closed_period_and_writes_nothing(db):
    sheet, row, period = _seeded_sheet(db)
    period.is_closed = True
    db.commit()
    overrides_before = _count_overrides(db)

    with pytest.raises(ValueError, match="closed period"):
        _do_clear_override(db, sheet.id, row, [period])

    assert _count_overrides(db) == overrides_before
