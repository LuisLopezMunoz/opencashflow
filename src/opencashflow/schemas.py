"""Pydantic request/response DTOs mirroring the ORM models in models.py --
Create/Update for input, Out for output, matching a typical HTTP-API-layer
shape (row_type/sign/etc. use plain str here, not opencashflow.enums'
Literal aliases, since these are meant to survive independently of the ORM
column types they mirror).

Nothing in this package calls into this module itself -- it's the shape
layer for an HTTP API built ON TOP of this package (e.g. serving a web
client, as opposed to a native/CLI consumer that can import the core
library's ORM models directly). Every *Out schema carries
from_attributes=True so a route handler can validate the core's own return
values directly -- an ORM row, or a result dataclass like
overrides.OverrideWriteResult / period_close.CloseReport -- with
SomeOut.model_validate(result), never by mapping fields by hand. See
test_schemas.py for coverage validating every schema here against a real
ORM instance or result dataclass.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, computed_field, model_validator

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
    # None = append after the sheet's last section (structure.add_section);
    # a fixed 0 default would silently reorder every existing section.
    sort_order: Optional[int] = None
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
    # None = append after the section's last row (structure.add_row).
    sort_order: Optional[int] = None
    is_visible: bool = True
    default_projection_rule: Optional[Dict[str, Any]] = None
    ledger_mapping: Optional[Dict[str, Any]] = None
    sign: str = "positive"
    notes: Optional[str] = None


_ROW_COLUMNS_NOT_NULL = ("name", "row_type", "sort_order", "is_visible", "sign")


class RowUpdate(BaseModel):
    """Partial update: only fields present in the request are applied (see
    `model_fields_set`). An explicit null clears default_projection_rule/
    ledger_mapping/notes; it's rejected for the columns that can't be null."""

    name: Optional[str] = None
    row_type: Optional[str] = None
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None
    default_projection_rule: Optional[Dict[str, Any]] = None
    ledger_mapping: Optional[Dict[str, Any]] = None
    sign: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _no_null_for_required_columns(self) -> "RowUpdate":
        nulled = [f for f in _ROW_COLUMNS_NOT_NULL if f in self.model_fields_set and getattr(self, f) is None]
        if nulled:
            raise ValueError(f"These fields can't be null: {', '.join(nulled)}.")
        return self


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


# ---------------------------------------------------------------------------
# Action request bodies -- an operation on existing objects, not a resource
# being created/updated, hence *Request rather than *Create/*Update.
# ---------------------------------------------------------------------------

class RecordUndoRequest(BaseModel):
    """Request body for POST .../cells/{row_id}/{period_id}/actual/undo."""

    note: Optional[str] = None


class RecordClearRequest(BaseModel):
    """Request body for POST .../rows/{row_id}/actual/clear -- every period
    is cleared in one transaction (record_ops.clear_record)."""

    period_ids: List[int] = Field(min_length=1)
    note: Optional[str] = None


class PeriodCloseRequest(BaseModel):
    """Request body for POST .../periods/{period_id}/close -- the
    period_close.close_period keyword arguments."""

    balance_row_id: Optional[int] = None  # None = find the sheet's running_balance row
    assume_unrecorded_as_pending: bool = False
    dry_run: bool = False


class PeriodReopenRequest(BaseModel):
    """Request body for POST .../periods/{period_id}/reopen."""

    balance_row_id: Optional[int] = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Backfilling historical periods
# ---------------------------------------------------------------------------

class PeriodsBackfillCreate(BaseModel):
    months: int  # how many months of history to add before the earliest existing period


class PeriodsBackfillOut(BaseModel):
    created: List[PeriodOut]
    skipped_existing: int


# ---------------------------------------------------------------------------
# Computed cell result (returned in memory by the engine)
# ---------------------------------------------------------------------------

class CellComputedOut(BaseModel):
    """Field-for-field the engine's own CellResult dataclass -- validate one
    directly with CellComputedOut.model_validate(cell_result)."""

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

    model_config = {"from_attributes": True}


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
    periods: List[PeriodOut]
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
    periods: List[PeriodOut]
    sections: List[MatrixSectionOut]


# ---------------------------------------------------------------------------
# Real-layer entries and record_ops results (opencashflow.record_ops)
# ---------------------------------------------------------------------------

class CellActualEntryOut(BaseModel):
    id: int
    cell_id: int
    actual_value: Optional[Decimal]
    accrued_value: Optional[Decimal]
    paid_value: Optional[Decimal]
    note: Optional[str]
    entry_kind: str
    created_by: int
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class RealValuesOut(BaseModel):
    """record_ops reports a cell's real values as a positional
    (actual, accrued, paid) tuple -- exposed here as named fields."""

    actual_value: Optional[Decimal]
    accrued_value: Optional[Decimal]
    paid_value: Optional[Decimal]

    @model_validator(mode="before")
    @classmethod
    def _from_tuple(cls, data: Any) -> Any:
        if isinstance(data, (tuple, list)):
            actual, accrued, paid = data
            return {"actual_value": actual, "accrued_value": accrued, "paid_value": paid}
        return data


class RecordSetResultOut(BaseModel):
    period: PeriodOut
    previous: RealValuesOut
    current: RealValuesOut
    entry: CellActualEntryOut

    model_config = {"from_attributes": True}


class RecordHistoryEntryOut(BaseModel):
    entry: CellActualEntryOut
    is_current: bool

    model_config = {"from_attributes": True}


class RecordUndoResultOut(BaseModel):
    period: PeriodOut
    reverted_from: CellActualEntryOut
    reverted_to: Optional[CellActualEntryOut]  # None means "back to no recorded value"
    new_entry: CellActualEntryOut

    model_config = {"from_attributes": True}


class RecordClearResultOut(BaseModel):
    period: PeriodOut
    cleared: bool
    previous: Optional[RealValuesOut]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Override results (opencashflow.overrides)
# ---------------------------------------------------------------------------

class OverrideWriteResultOut(BaseModel):
    period: PeriodOut
    override: Optional[CellOverrideOut]  # None only when a lock write was skipped
    previous_override: Optional[CellOverrideOut]
    skipped: bool
    skip_reason: Optional[str]

    model_config = {"from_attributes": True}


class OverrideClearResultOut(BaseModel):
    period: PeriodOut
    previous_override: Optional[CellOverrideOut]  # None means nothing was active to clear
    reverted_value: Optional[Decimal]
    reverted_source: Optional[str]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Period close / reopen reports (opencashflow.period_close)
# ---------------------------------------------------------------------------

class RolloverEntryOut(BaseModel):
    row_name: str
    pending_amount: Decimal
    disposition: str

    model_config = {"from_attributes": True}


class CloseReportOut(BaseModel):
    sheet_id: int
    period_id: int
    period_label: str
    balance_row_name: str
    balance_final_row_name: str
    saldo_inicial_value: Decimal
    real_net_flow: Decimal
    saldo_final_value: Decimal
    rollovers: List[RolloverEntryOut]
    warnings: List[str]
    next_period_label: Optional[str]
    dry_run: bool

    model_config = {"from_attributes": True}


class ReopenReportOut(BaseModel):
    sheet_id: int
    period_id: int
    period_label: str
    balance_final_row_name: str
    touched_notes: List[str]
    dry_run: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Wallet / WalletMovement (opencashflow.wallet, opencashflow.wallet_movements)
# ---------------------------------------------------------------------------

class WalletCreate(BaseModel):
    name: str
    wallet_type: str = "cash"
    currency: str = "USD"
    balance: Decimal = Decimal("0")
    description: Optional[str] = None


class WalletUpdate(BaseModel):
    """Partial update -- only fields present in the request are applied
    (use model_fields_set to tell "omitted" apart from "explicitly null")."""

    name: Optional[str] = None
    wallet_type: Optional[str] = None
    currency: Optional[str] = None
    balance: Optional[Decimal] = None
    description: Optional[str] = None


class WalletOut(BaseModel):
    id: int
    user_id: int
    name: str
    wallet_type: str
    currency: str
    balance: Decimal
    description: Optional[str]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class WalletMovementOut(BaseModel):
    id: int
    wallet_id: int
    amount: Decimal  # signed: positive grew the wallet, negative shrank it
    sheet_id: int
    row_id: int
    period_id: int
    actual_entry_id: int
    reverses_movement_id: Optional[int]
    note: Optional[str]
    created_by: Optional[int]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class WalletMovementResultOut(BaseModel):
    movement: WalletMovementOut
    wallet_balance_before: Decimal
    wallet_balance_after: Decimal
    cell_paid_before: Optional[Decimal]
    cell_paid_after: Decimal

    model_config = {"from_attributes": True}


class WalletMovementUndoResultOut(BaseModel):
    original: WalletMovementOut
    reversal: WalletMovementOut
    wallet_balance_before: Decimal
    wallet_balance_after: Decimal

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Credit cards (opencashflow.credit_card, opencashflow.creditcard_statements)
# ---------------------------------------------------------------------------

class CreditCardCreate(BaseModel):
    name: str
    bank: Optional[str] = None
    credit_limit: Decimal
    currency: str = "USD"
    closing_day: Optional[int] = None
    due_day: Optional[int] = None
    interest_rate: Decimal = Decimal("0")
    minimum_payment_rate: Decimal = Decimal("0.05")
    # Never more than the last 4 digits -- a full card number must be
    # rejected here, at the boundary, not trusted to the caller.
    last4: Optional[str] = Field(default=None, pattern=r"^\d{4}$")
    expiration_date: Optional[date] = None
    network: Optional[str] = None
    holder_name: Optional[str] = None
    status: str = "active"


class CreditCardUpdate(BaseModel):
    """Partial update -- see WalletUpdate."""

    name: Optional[str] = None
    bank: Optional[str] = None
    credit_limit: Optional[Decimal] = None
    currency: Optional[str] = None
    closing_day: Optional[int] = None
    due_day: Optional[int] = None
    interest_rate: Optional[Decimal] = None
    minimum_payment_rate: Optional[Decimal] = None
    last4: Optional[str] = Field(default=None, pattern=r"^\d{4}$")
    expiration_date: Optional[date] = None
    network: Optional[str] = None
    holder_name: Optional[str] = None
    status: Optional[str] = None


class CreditCardOut(BaseModel):
    """Deliberately omits CreditCard.current_balance -- deprecated on the
    model itself, never read by any computation in this package; a new API
    surface shouldn't start exposing it."""

    id: int
    user_id: int
    name: str
    bank: Optional[str]
    credit_limit: Decimal
    currency: str
    closing_day: Optional[int]
    due_day: Optional[int]
    interest_rate: Decimal
    minimum_payment_rate: Decimal
    mapped_sheet_id: Optional[int]
    mapped_row_id: Optional[int]
    last4: Optional[str]
    expiration_date: Optional[date]
    network: Optional[str]
    holder_name: Optional[str]
    status: str
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class CreditCardChargeCreate(BaseModel):
    amount: Decimal  # the WHOLE purchase's total, never a per-installment amount
    description: Optional[str] = None
    category: Optional[str] = None
    charge_date: Optional[date] = None  # None = today
    installments: int = Field(default=1, ge=1)
    first_statement_period: Optional[date] = None


class CreditCardChargeOut(BaseModel):
    id: int
    credit_card_id: int
    amount: Decimal
    description: Optional[str]
    category: Optional[str]
    charge_date: date
    installments: int
    installments_paid: int
    first_statement_period: Optional[date]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class CreditCardStatementLineCreate(BaseModel):
    line_type: str
    description: str
    transaction_date: date
    amount: Decimal  # THIS line's amount (per installment, when applicable)
    installment_number: Optional[int] = None
    installment_total: Optional[int] = None
    charge_id: Optional[int] = None
    category: Optional[str] = None


class CreditCardStatementLineOut(BaseModel):
    id: int
    statement_id: int
    charge_id: Optional[int]
    line_type: str
    installment_number: Optional[int]
    installment_total: Optional[int]
    description: str
    category: Optional[str]
    transaction_date: date
    amount: Decimal
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class CreditCardStatementCreate(BaseModel):
    billing_period: date
    closing_date: date
    due_date: date
    new_balance: Decimal
    total_payment_due: Decimal
    previous_balance: Optional[Decimal] = None
    payments_received: Optional[Decimal] = None
    new_charges_total: Optional[Decimal] = None
    interest_charged: Optional[Decimal] = None
    other_charges: Optional[Decimal] = None
    minimum_payment: Optional[Decimal] = None
    remaining_principal_installments: Optional[Decimal] = None
    currency: str = "USD"
    notes: Optional[str] = None


class CreditCardStatementOut(BaseModel):
    id: int
    credit_card_id: int
    billing_period: date
    previous_balance: Optional[Decimal]
    payments_received: Optional[Decimal]
    new_charges_total: Optional[Decimal]
    interest_charged: Optional[Decimal]
    other_charges: Optional[Decimal]
    new_balance: Decimal
    minimum_payment: Optional[Decimal]
    total_payment_due: Decimal
    remaining_principal_installments: Optional[Decimal]
    closing_date: date
    due_date: date
    currency: str
    source: str
    source_file: Optional[str]
    notes: Optional[str]
    created_by: Optional[int]
    created_at: Optional[datetime]
    lines: List[CreditCardStatementLineOut]

    model_config = {"from_attributes": True}


class InstantStatementLineOut(BaseModel):
    description: str
    amount: Decimal
    installment_number: Optional[int]
    installment_total: Optional[int]
    is_projected: bool

    model_config = {"from_attributes": True}


class InstantStatementOut(BaseModel):
    card: CreditCardOut
    period: date  # the PAYMENT month, not the billing month
    previous_balance: Optional[Decimal]
    previous_balance_known: bool
    is_real: bool
    lines: List[InstantStatementLineOut]
    charges_due_this_cycle: Decimal
    interest: Optional[Decimal]
    minimum_payment: Optional[Decimal]

    model_config = {"from_attributes": True}


class SyncReportOut(BaseModel):
    card_id: int
    card_name: str
    sheet_id: int
    row_id: int
    period: date
    branch: str  # "real_statement" | "projection"
    old_value: Optional[Decimal]
    new_value: Decimal
    note: str
    dry_run: bool

    model_config = {"from_attributes": True}
