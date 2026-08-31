"""Period generation for a CashflowSheet."""
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from opencashflow.models import CashflowSheet, SheetPeriod


def generate_periods(sheet: CashflowSheet, db: Session) -> None:
    """Create SheetPeriod rows for each month in the sheet's horizon."""
    base = sheet.base_period
    year, month = base.year, base.month
    for i in range(sheet.horizon_months):
        m = (month - 1 + i) % 12 + 1
        y = year + (month - 1 + i) // 12
        period_date = datetime(y, m, 1)
        label = period_date.strftime("%b %Y")
        period = SheetPeriod(
            sheet_id=sheet.id,
            period_date=period_date,
            label=label,
            is_closed=False,
            sort_order=i,
        )
        db.add(period)
    db.flush()


def extend_periods_backward(sheet: CashflowSheet, months: int, db: Session) -> List[SheetPeriod]:
    """Add up to `months` historical SheetPeriod rows immediately before the
    earliest period the sheet already has.

    The pivot is whatever period currently has the minimum sort_order — NOT
    sheet.base_period, which is left untouched and keeps meaning exactly
    what it always has (where generate_periods started counting forward).
    This makes the function composable: calling it twice (months=5, then
    months=3) yields 8 contiguous months of history with no manual date
    bookkeeping by the caller, and repeated/partial calls stay correct even
    if earlier calls left gaps (see idempotency below).

    Defense-in-depth: a candidate month whose period_date already exists on
    the sheet is skipped, never duplicated (protects the (sheet_id,
    period_date) unique constraint). This should not come up through normal
    sequential use — since the pivot is always the CURRENT minimum
    sort_order, a later call naturally continues further back rather than
    re-requesting dates an earlier call already claimed. It only matters if
    a period in the requested range was created some other way.

    Created periods are marked is_closed=True (they're calendar history by
    definition) and are returned newest-first (created[0] is the month
    immediately before the old earliest; created[-1] is the furthest back).
    Does not create SheetCell rows — cells are always materialized on
    demand, same as everywhere else in this package.
    """
    existing = sorted(sheet.periods, key=lambda p: p.sort_order)
    if not existing:
        raise ValueError("Sheet has no periods yet — call generate_periods() first.")

    earliest = existing[0]
    existing_dates = {p.period_date for p in existing}

    year, month = earliest.period_date.year, earliest.period_date.month
    next_sort_order = earliest.sort_order - 1
    created: List[SheetPeriod] = []

    for _ in range(months):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        period_date = datetime(year, month, 1)
        if period_date not in existing_dates:
            period = SheetPeriod(
                sheet_id=sheet.id,
                period_date=period_date,
                label=period_date.strftime("%b %Y"),
                is_closed=True,
                sort_order=next_sort_order,
            )
            db.add(period)
            created.append(period)
            existing_dates.add(period_date)
        next_sort_order -= 1

    db.flush()
    return created


def find_anchor_period(periods: List[SheetPeriod], today: Optional[datetime] = None) -> Optional[SheetPeriod]:
    """Pick the period to treat as "now" for relative period-window views
    (e.g. show --before/--after/--context).

    1. A period whose (year, month) matches `today`, if the sheet has one.
    2. Otherwise the earliest period strictly after today (closest future).
    3. Otherwise the latest period available (the whole sheet is in the past).
    Never fails as long as `periods` is non-empty; returns None only then.
    """
    if not periods:
        return None
    if today is None:
        today = datetime.utcnow()
    ordered = sorted(periods, key=lambda p: p.sort_order)
    for p in ordered:
        if p.period_date.year == today.year and p.period_date.month == today.month:
            return p
    for p in ordered:
        if p.period_date > today:
            return p
    return ordered[-1]
