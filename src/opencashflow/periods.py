"""Period generation for a CashflowSheet."""
from datetime import datetime

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
