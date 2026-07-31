from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, model_validator


# ---------------------------------------------------------------------------
# CashflowSheet
# ---------------------------------------------------------------------------

class SheetCreate(BaseModel):
    name: str
    currency: str = "USD"
    horizon_months: int = 12
    base_period: datetime  # caller sends the first day of the first month


class SheetOut(BaseModel):
    id: int
    user_id: int
    name: str
    currency: str
    horizon_months: int
    base_period: datetime
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# SheetSection
# ---------------------------------------------------------------------------

class SectionCreate(BaseModel):
    name: str
    section_type: str = "custom"
    sort_order: int = 0
    is_collapsible: bool = True
    color_hex: Optional[str] = None


class SectionOut(BaseModel):
    id: int
    sheet_id: int
    name: str
    section_type: str
    sort_order: int
    is_collapsible: bool
    color_hex: Optional[str]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# SheetRow
# ---------------------------------------------------------------------------

class RowCreate(BaseModel):
    name: str
    row_type: str = "input"
    sort_order: int = 0
    is_visible: bool = True
    default_projection_rule: Optional[Dict[str, Any]] = None
    ledger_mapping: Optional[Dict[str, Any]] = None
    sign: str = "positive"
    notes: Optional[str] = None


class RowUpdate(BaseModel):
    name: Optional[str] = None
    row_type: Optional[str] = None
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None
    default_projection_rule: Optional[Dict[str, Any]] = None
    ledger_mapping: Optional[Dict[str, Any]] = None
    sign: Optional[str] = None
    notes: Optional[str] = None


class RowOut(BaseModel):
    id: int
    section_id: int
    name: str
    row_type: str
    sort_order: int
    is_visible: bool
    default_projection_rule: Optional[Dict[str, Any]]
    ledger_mapping: Optional[Dict[str, Any]]
    sign: str
    notes: Optional[str]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# SheetPeriod
# ---------------------------------------------------------------------------

class PeriodOut(BaseModel):
    id: int
    sheet_id: int
    period_date: datetime
    label: Optional[str]
    is_closed: bool
    is_current: bool
    sort_order: int

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def compute_is_current(cls, data: Any) -> Any:
        """is_current is computed dynamically: true when period_date is in the current month."""
        if hasattr(data, "__dict__"):
            period_date: Optional[datetime] = getattr(data, "period_date", None)
            if period_date is not None:
                today = datetime.utcnow()
                object.__setattr__(
                    data,
                    "_is_current",
                    period_date.year == today.year and period_date.month == today.month,
                )
        return data


class PeriodOutSimple(BaseModel):
    """Simplified period schema without dynamic is_current computation for use in matrix."""

    id: int
    sheet_id: int
    period_date: datetime
    label: Optional[str]
    is_closed: bool
    sort_order: int

    model_config = {"from_attributes": True}

    @property
    def is_current(self) -> bool:
        today = datetime.utcnow()
        return self.period_date.year == today.year and self.period_date.month == today.month


# ---------------------------------------------------------------------------
# SheetCell override input
# ---------------------------------------------------------------------------

class CellOverrideCreate(BaseModel):
    """Request body for PUT /sheets/{id}/cells/{row_id}/{period_id}.

    Provide either `value` (manual_value) or `custom_rule` (manual_rule), not both.
    If both are omitted, a `lock` override is created.
    """

    value: Optional[Decimal] = None
    custom_rule: Optional[Dict[str, Any]] = None
    note: Optional[str] = None

    @model_validator(mode="after")
    def check_value_or_rule(self) -> "CellOverrideCreate":
        if self.value is not None and self.custom_rule is not None:
            raise ValueError("Provide either 'value' or 'custom_rule', not both.")
        return self

    @property
    def override_type(self) -> str:
        if self.value is not None:
            return "manual_value"
        if self.custom_rule is not None:
            return "manual_rule"
        return "lock"


class CellOverrideOut(BaseModel):
    id: int
    cell_id: int
    value: Optional[Decimal]
    override_type: str
    custom_rule: Optional[Dict[str, Any]]
    note: Optional[str]
    created_by: int
    created_at: datetime
    superseded_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Computed cell result (returned in memory by the engine)
# ---------------------------------------------------------------------------

class CellComputedOut(BaseModel):
    row_id: int
    period_id: int
    projected_value: Optional[Decimal]
    actual_value: Optional[Decimal]
    accrued_value: Optional[Decimal]
    paid_value: Optional[Decimal]
    pending_value: Optional[Decimal]
    variance: Optional[Decimal]
    effective_source: Optional[str]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Full sheet detail (sections + rows + periods)
# ---------------------------------------------------------------------------

class SectionWithRowsOut(BaseModel):
    id: int
    sheet_id: int
    name: str
    section_type: str
    sort_order: int
    is_collapsible: bool
    color_hex: Optional[str]
    rows: List[RowOut]

    model_config = {"from_attributes": True}


class SheetDetailOut(BaseModel):
    id: int
    user_id: int
    name: str
    currency: str
    horizon_months: int
    base_period: datetime
    created_at: datetime
    updated_at: datetime
    periods: List[PeriodOutSimple]
    sections: List[SectionWithRowsOut]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Matrix response
# ---------------------------------------------------------------------------

class MatrixRowOut(BaseModel):
    row: RowOut
    cells: List[CellComputedOut]


class MatrixSectionOut(BaseModel):
    section: SectionOut
    rows: List[MatrixRowOut]


class MatrixOut(BaseModel):
    sheet: SheetOut
    periods: List[PeriodOutSimple]
    sections: List[MatrixSectionOut]
