"""API router for the cashflow sheet engine.

Endpoints:
  POST   /api/sheets                                      Create a sheet (auto-generates periods)
  GET    /api/sheets                                      List user's sheets
  GET    /api/sheets/{sheet_id}                           Sheet detail with sections, rows and periods
  POST   /api/sheets/{sheet_id}/sections                  Add a section
  POST   /api/sheets/{sheet_id}/sections/{section_id}/rows  Add a row to a section
  PATCH  /api/sheets/{sheet_id}/sections/{section_id}/rows/{row_id}  Update a row
  GET    /api/sheets/{sheet_id}/matrix                    Full calculated matrix
  PUT    /api/sheets/{sheet_id}/cells/{row_id}/{period_id}   Write/replace a cell override
  DELETE /api/sheets/{sheet_id}/cells/{row_id}/{period_id}/override  Remove active override
"""

import calendar
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.cashflow.engine import compute_sheet
from backend.cashflow.models import (
    CashflowSheet,
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    SheetSection,
)
from backend.cashflow.schemas import (
    CellComputedOut,
    CellOverrideCreate,
    CellOverrideOut,
    MatrixOut,
    MatrixRowOut,
    MatrixSectionOut,
    PeriodOutSimple,
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
from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models.user import User

router = APIRouter(prefix="/api/sheets", tags=["cashflow-sheets"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_sheet_or_404(sheet_id: int, user_id: int, db: Session) -> CashflowSheet:
    sheet = db.query(CashflowSheet).filter(
        CashflowSheet.id == sheet_id,
        CashflowSheet.user_id == user_id,
    ).first()
    if not sheet:
        raise HTTPException(status_code=404, detail="Sheet not found")
    return sheet


def _generate_periods(sheet: CashflowSheet, db: Session) -> None:
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


def _period_is_current(period_date: datetime) -> bool:
    today = datetime.utcnow()
    return period_date.year == today.year and period_date.month == today.month


def _get_or_create_cell(row_id: int, period_id: int, db: Session) -> SheetCell:
    cell = db.query(SheetCell).filter(
        SheetCell.row_id == row_id,
        SheetCell.period_id == period_id,
    ).first()
    if not cell:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    return cell


def _verify_row_in_sheet(row_id: int, sheet_id: int, db: Session) -> SheetRow:
    row = (
        db.query(SheetRow)
        .join(SheetSection)
        .filter(SheetRow.id == row_id, SheetSection.sheet_id == sheet_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Row not found in this sheet")
    return row


def _verify_period_in_sheet(period_id: int, sheet_id: int, db: Session) -> SheetPeriod:
    period = db.query(SheetPeriod).filter(
        SheetPeriod.id == period_id,
        SheetPeriod.sheet_id == sheet_id,
    ).first()
    if not period:
        raise HTTPException(status_code=404, detail="Period not found in this sheet")
    return period


# ---------------------------------------------------------------------------
# Sheet endpoints
# ---------------------------------------------------------------------------

@router.post("/", response_model=SheetOut, status_code=status.HTTP_201_CREATED)
def create_sheet(
    sheet_in: SheetCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a cashflow sheet and auto-generate its monthly periods."""
    # Normalize base_period to first day of month
    bp = sheet_in.base_period
    base_period = datetime(bp.year, bp.month, 1)

    sheet = CashflowSheet(
        user_id=current_user.id,
        name=sheet_in.name,
        currency=sheet_in.currency.upper(),
        horizon_months=sheet_in.horizon_months,
        base_period=base_period,
    )
    db.add(sheet)
    db.flush()
    _generate_periods(sheet, db)
    db.commit()
    db.refresh(sheet)
    return sheet


@router.get("/", response_model=List[SheetOut])
def list_sheets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(CashflowSheet)
        .filter(CashflowSheet.user_id == current_user.id)
        .order_by(CashflowSheet.created_at.desc())
        .all()
    )


@router.get("/{sheet_id}", response_model=SheetDetailOut)
def get_sheet(
    sheet_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return sheet with all sections, rows and periods (no computed values)."""
    sheet = _get_sheet_or_404(sheet_id, current_user.id, db)
    return sheet


# ---------------------------------------------------------------------------
# Section endpoints
# ---------------------------------------------------------------------------

@router.post("/{sheet_id}/sections", response_model=SectionOut, status_code=status.HTTP_201_CREATED)
def add_section(
    sheet_id: int,
    section_in: SectionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_sheet_or_404(sheet_id, current_user.id, db)
    section = SheetSection(sheet_id=sheet_id, **section_in.model_dump())
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


# ---------------------------------------------------------------------------
# Row endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/{sheet_id}/sections/{section_id}/rows",
    response_model=RowOut,
    status_code=status.HTTP_201_CREATED,
)
def add_row(
    sheet_id: int,
    section_id: int,
    row_in: RowCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_sheet_or_404(sheet_id, current_user.id, db)
    section = db.query(SheetSection).filter(
        SheetSection.id == section_id,
        SheetSection.sheet_id == sheet_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    row = SheetRow(section_id=section_id, **row_in.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch(
    "/{sheet_id}/sections/{section_id}/rows/{row_id}",
    response_model=RowOut,
)
def update_row(
    sheet_id: int,
    section_id: int,
    row_id: int,
    row_update: RowUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_sheet_or_404(sheet_id, current_user.id, db)
    row = db.query(SheetRow).filter(
        SheetRow.id == row_id,
        SheetRow.section_id == section_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    for field, value in row_update.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Matrix endpoint
# ---------------------------------------------------------------------------

@router.get("/{sheet_id}/matrix", response_model=MatrixOut)
def get_matrix(
    sheet_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the fully calculated matrix for the sheet."""
    sheet = _get_sheet_or_404(sheet_id, current_user.id, db)

    result = compute_sheet(sheet_id, db)
    periods = result["periods"]
    raw_sections = result["sections"]

    period_schemas = [
        PeriodOutSimple.model_validate(p) for p in periods
    ]

    matrix_sections = []
    for sec_data in raw_sections:
        section = sec_data["section"]
        matrix_rows = []
        for row_data in sec_data["rows"]:
            row = row_data["row"]
            cells = [
                CellComputedOut(
                    row_id=cr.row_id,
                    period_id=cr.period_id,
                    projected_value=cr.projected_value,
                    actual_value=cr.actual_value,
                    accrued_value=cr.accrued_value,
                    paid_value=cr.paid_value,
                    pending_value=cr.pending_value,
                    variance=cr.variance,
                    effective_source=cr.effective_source,
                    error=cr.error,
                )
                for cr in row_data["cells"]
            ]
            matrix_rows.append(MatrixRowOut(row=RowOut.model_validate(row), cells=cells))
        matrix_sections.append(
            MatrixSectionOut(section=SectionOut.model_validate(section), rows=matrix_rows)
        )

    return MatrixOut(
        sheet=SheetOut.model_validate(sheet),
        periods=period_schemas,
        sections=matrix_sections,
    )


# ---------------------------------------------------------------------------
# Cell override endpoints
# ---------------------------------------------------------------------------

@router.put(
    "/{sheet_id}/cells/{row_id}/{period_id}",
    response_model=CellOverrideOut,
    status_code=status.HTTP_200_OK,
)
def write_cell_override(
    sheet_id: int,
    row_id: int,
    period_id: int,
    override_in: CellOverrideCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Write (or replace) a manual override for a cell.

    If a previous active override exists, it is marked as superseded.
    """
    _get_sheet_or_404(sheet_id, current_user.id, db)
    _verify_row_in_sheet(row_id, sheet_id, db)
    _verify_period_in_sheet(period_id, sheet_id, db)

    cell = _get_or_create_cell(row_id, period_id, db)

    # Supersede any existing active override
    existing = (
        db.query(CellOverride)
        .filter(CellOverride.cell_id == cell.id, CellOverride.superseded_at.is_(None))
        .first()
    )
    if existing:
        existing.superseded_at = datetime.utcnow()
        db.flush()

    override = CellOverride(
        cell_id=cell.id,
        value=override_in.value,
        override_type=override_in.override_type,
        custom_rule=override_in.custom_rule,
        note=override_in.note,
        created_by=current_user.id,
    )
    db.add(override)
    db.commit()
    db.refresh(override)
    return override


@router.delete(
    "/{sheet_id}/cells/{row_id}/{period_id}/override",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_cell_override(
    sheet_id: int,
    row_id: int,
    period_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove the active override for a cell, reverting to the row's projection rule."""
    _get_sheet_or_404(sheet_id, current_user.id, db)
    _verify_row_in_sheet(row_id, sheet_id, db)
    _verify_period_in_sheet(period_id, sheet_id, db)

    cell = db.query(SheetCell).filter(
        SheetCell.row_id == row_id,
        SheetCell.period_id == period_id,
    ).first()
    if not cell:
        return  # Nothing to delete

    active = (
        db.query(CellOverride)
        .filter(CellOverride.cell_id == cell.id, CellOverride.superseded_at.is_(None))
        .first()
    )
    if active:
        active.superseded_at = datetime.utcnow()
        db.commit()
