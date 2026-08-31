"""Unit tests for opencashflow.periods: generate_periods (existing, smoke
only) plus the newer extend_periods_backward and find_anchor_period.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.models import Base, CashflowSheet, SheetPeriod
from opencashflow.periods import extend_periods_backward, find_anchor_period, generate_periods

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


def _make_sheet(db, months=3, base_period=datetime(2026, 9, 1)) -> CashflowSheet:
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Periods Test Sheet", currency="CLP",
                           horizon_months=months, base_period=base_period)
    db.add(sheet)
    db.flush()
    generate_periods(sheet, db)
    db.commit()
    return sheet


def _dates(periods):
    return [p.period_date for p in periods]


# ---------------------------------------------------------------------------
# extend_periods_backward
# ---------------------------------------------------------------------------

def test_extend_periods_backward_creates_contiguous_history(db):
    sheet = _make_sheet(db, months=2, base_period=datetime(2026, 9, 1))  # Sep, Oct 2026

    created = extend_periods_backward(sheet, months=3, db=db)
    db.commit()

    assert _dates(created) == [datetime(2026, 8, 1), datetime(2026, 7, 1), datetime(2026, 6, 1)]
    for p in created:
        assert p.is_closed is True

    all_periods = sorted(sheet.periods, key=lambda p: p.sort_order)
    assert _dates(all_periods) == [
        datetime(2026, 6, 1), datetime(2026, 7, 1), datetime(2026, 8, 1),
        datetime(2026, 9, 1), datetime(2026, 10, 1),
    ]
    # sort_order strictly increasing with period_date, no collisions.
    sort_orders = [p.sort_order for p in all_periods]
    assert sort_orders == sorted(sort_orders)
    assert len(set(sort_orders)) == len(sort_orders)


def test_extend_periods_backward_crosses_year_boundary(db):
    sheet = _make_sheet(db, months=1, base_period=datetime(2026, 1, 1))  # Jan 2026 only

    created = extend_periods_backward(sheet, months=2, db=db)
    db.commit()

    assert _dates(created) == [datetime(2025, 12, 1), datetime(2025, 11, 1)]


def test_extend_periods_backward_skips_a_date_that_already_exists(db):
    """Defense-in-depth, not a normal-use path: the pivot (current minimum
    sort_order) is recomputed fresh on every call, so under legitimate
    sequential use a later call never re-requests a month an earlier call
    already claimed — it just continues further back (see the composability
    test above). The in-loop "skip if this date already exists" check only
    matters if a period for a date within the walk shows up some OTHER way
    (a concurrent/retried call, a manual insert) — which, for it to not
    simply become the new pivot itself, requires a sort_order that doesn't
    correlate with its date. That inconsistency is deliberately constructed
    here (sort_order=50 despite an earlier date) purely to exercise this
    one safety check in isolation, not to model anything that should occur
    through normal use of this function alone.
    """
    sheet = _make_sheet(db, months=1, base_period=datetime(2026, 9, 1))
    db.add(SheetPeriod(sheet_id=sheet.id, period_date=datetime(2026, 8, 1),
                        label="Aug 2026 (pre-existing)", is_closed=True, sort_order=50))
    db.commit()

    created = extend_periods_backward(sheet, months=2, db=db)  # would be Aug, Jul
    db.commit()

    # Aug was skipped (already existed); Jul was created normally.
    assert _dates(created) == [datetime(2026, 7, 1)]

    all_dates = sorted(_dates(sheet.periods))
    assert all_dates == [datetime(2026, 7, 1), datetime(2026, 8, 1), datetime(2026, 9, 1)]
    assert len(all_dates) == len(set(all_dates))  # no duplicate Aug row


def test_extend_periods_backward_is_composable_across_successive_calls(db):
    sheet = _make_sheet(db, months=1, base_period=datetime(2026, 9, 1))

    extend_periods_backward(sheet, months=2, db=db)  # Aug, Jul
    db.commit()
    extend_periods_backward(sheet, months=2, db=db)  # Jun, May (from the new earliest: Jul)
    db.commit()

    all_periods = sorted(sheet.periods, key=lambda p: p.sort_order)
    assert _dates(all_periods) == [
        datetime(2026, 5, 1), datetime(2026, 6, 1), datetime(2026, 7, 1),
        datetime(2026, 8, 1), datetime(2026, 9, 1),
    ]


def test_extend_periods_backward_does_not_touch_base_period_or_horizon(db):
    sheet = _make_sheet(db, months=2, base_period=datetime(2026, 9, 1))
    original_base_period = sheet.base_period
    original_horizon = sheet.horizon_months

    extend_periods_backward(sheet, months=3, db=db)
    db.commit()

    assert sheet.base_period == original_base_period
    assert sheet.horizon_months == original_horizon


def test_extend_periods_backward_raises_without_existing_periods(db):
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Empty", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.commit()

    with pytest.raises(ValueError):
        extend_periods_backward(sheet, months=1, db=db)


# ---------------------------------------------------------------------------
# find_anchor_period
# ---------------------------------------------------------------------------

def test_find_anchor_period_matches_current_month(db):
    sheet = _make_sheet(db, months=3, base_period=datetime(2026, 8, 1))  # Aug, Sep, Oct 2026
    periods = sheet.periods

    anchor = find_anchor_period(periods, today=datetime(2026, 9, 15))

    assert anchor.period_date == datetime(2026, 9, 1)


def test_find_anchor_period_falls_back_to_closest_future(db):
    sheet = _make_sheet(db, months=3, base_period=datetime(2026, 9, 1))  # Sep, Oct, Nov 2026
    periods = sheet.periods

    # "Today" is before the sheet even starts.
    anchor = find_anchor_period(periods, today=datetime(2026, 6, 1))

    assert anchor.period_date == datetime(2026, 9, 1)


def test_find_anchor_period_falls_back_to_latest_when_all_in_past(db):
    sheet = _make_sheet(db, months=3, base_period=datetime(2020, 1, 1))  # Jan-Mar 2020
    periods = sheet.periods

    anchor = find_anchor_period(periods, today=datetime(2026, 9, 15))

    assert anchor.period_date == datetime(2020, 3, 1)


def test_find_anchor_period_returns_none_for_empty_list():
    assert find_anchor_period([], today=datetime(2026, 9, 15)) is None
