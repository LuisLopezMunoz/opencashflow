"""Pydantic request/response DTOs mirroring the ORM models in models.py --
Create/Update for input, Out for output, matching a typical HTTP-API-layer
shape (row_type/sign/etc. use plain str here, not opencashflow.enums'
Literal aliases, since these are meant to survive independently of the ORM
column types they mirror).

Nothing in this package or in opencashflow-cli calls into this module
today -- it isn't wired into any HTTP framework, router, or endpoint. It's
kept as a reserved starting point for a future API layer (e.g. serving a
web client, as opposed to a native/CLI consumer that can import the core
library's ORM models directly) rather than deleted, since its shapes are
already most of what such a layer would need to define for itself. See
test_schemas.py for coverage validating every schema here against a real
ORM instance where one exists.
"""
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, computed_field, model_validator

from opencashflow.periods import is_same_month


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
    sort_order: int

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_current(self) -> bool:
        """True when period_date is in the current month, computed dynamically
        rather than stored -- "now" is meaningless to persist on a row."""
        return is_same_month(self.period_date, datetime.utcnow())


class PeriodOutSimple(BaseModel):
    """Simplified period schema without dynamic is_current computation for use in matrix."""

    id: int
    sheet_id: int
    period_date: datetime
    label: Optional[str]
    is_closed: bool
    sort_order: int

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_current(self) -> bool:
        return is_same_month(self.period_date, datetime.utcnow())


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
# SheetCell real-layer input (actual_value/accrued_value/paid_value)
# ---------------------------------------------------------------------------

class CellActualUpdate(BaseModel):
    """Request body for PATCH /sheets/{id}/cells/{row_id}/{period_id}/actual.

    All three fields are independent — none is inferred from another. Only
    fields actually present in the request are applied (use
    `model_fields_set` to tell "omitted" apart from "explicitly null", so a
    caller can clear a single field without touching the other two).
    """

    actual_value: Optional[Decimal] = None
    accrued_value: Optional[Decimal] = None
    paid_value: Optional[Decimal] = None
    note: Optional[str] = None


class CellActualOut(BaseModel):
    row_id: int
    period_id: int
    actual_value: Optional[Decimal]
    accrued_value: Optional[Decimal]
    paid_value: Optional[Decimal]
    # Derived here (accrued - paid) as a convenience; not persisted on the
    # cell — same convention as the engine's own in-memory-only derivation.
    pending_value: Optional[Decimal]
    updated_by: int
    updated_at: datetime


# ---------------------------------------------------------------------------
# Backfilling historical periods
# ---------------------------------------------------------------------------

class PeriodsBackfillCreate(BaseModel):
    months: int  # how many months of history to add before the earliest existing period


class PeriodsBackfillOut(BaseModel):
    created: List[PeriodOutSimple]
    skipped_existing: int


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
