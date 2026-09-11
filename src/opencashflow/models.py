from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, validates

from opencashflow.enums import ENTRY_KINDS, OVERRIDE_TYPES, ROW_SIGNS, ROW_TYPES, SECTION_TYPES

# Owned by this package: opencashflow has no dependency on a host app's
# users/auth/ledger tables or on their SQLAlchemy registry. See models below
# for how ownership (user_id/created_by) is represented without a ForeignKey.
# DeclarativeBase (not the legacy declarative_base()) so mypy can resolve it
# as a valid base class -- everything below uses SQLAlchemy 2.0's Mapped[]/
# mapped_column() style, which DeclarativeBase is what makes type-checkable
# at all (the legacy Column()-only style leaves every instance attribute
# typed as Column[X] instead of X to mypy, no matter what Base is).
class Base(DeclarativeBase):
    pass


class CashflowSheet(Base):
    """Root spreadsheet. Belongs to one user, defines the time horizon and base currency."""

    __tablename__ = "cashflow_sheets"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Plain owner id, not a ForeignKey: this package doesn't own (or know about)
    # the users table. Referential integrity is the consuming app's job.
    user_id: Mapped[int] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    horizon_months: Mapped[int] = mapped_column(nullable=False, default=12)
    # First day of the first period (always day=1 of a month)
    base_period: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # cascade="save-update, merge" (no delete-orphan): sections/periods --
    # and everything beneath them (rows, cells, overrides, actual_entries)
    # -- are never meant to disappear just because their parent sheet is
    # deleted from a session. CellOverride/CellActualEntry are explicitly
    # documented as append-only audit trails; letting an ORM-level cascade
    # silently destroy that trail the moment someone deletes a CashflowSheet
    # would contradict that guarantee. No code path calls db.delete() on any
    # of these today, so this is preventive: an eventual delete/archive
    # feature should use an explicit soft-delete flag instead.
    sections: Mapped[List["SheetSection"]] = relationship(
        back_populates="sheet", cascade="save-update, merge", passive_deletes=True,
        order_by="SheetSection.sort_order",
    )
    periods: Mapped[List["SheetPeriod"]] = relationship(
        back_populates="sheet", cascade="save-update, merge", passive_deletes=True,
        order_by="SheetPeriod.sort_order",
    )


class SheetSection(Base):
    """Visual grouping of rows within a sheet (e.g. 'Income', 'Fixed Expenses')."""

    __tablename__ = "sheet_sections"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("cashflow_sheets.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # income | expense | financing | balance | custom
    section_type: Mapped[str] = mapped_column(String(20), nullable=False, default="custom")
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)
    is_collapsible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    color_hex: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)

    sheet: Mapped["CashflowSheet"] = relationship(back_populates="sections")
    # No delete-orphan -- see CashflowSheet.sections' comment: a section's
    # rows (and their cells/overrides/actual_entries) must survive the
    # section being deleted from a session, not cascade away with it.
    rows: Mapped[List["SheetRow"]] = relationship(
        back_populates="section", cascade="save-update, merge", passive_deletes=True,
        order_by="SheetRow.sort_order",
    )

    @validates("section_type")
    def _validate_section_type(self, key: str, value: str) -> str:
        if value not in SECTION_TYPES:
            raise ValueError(
                f"section_type debe ser uno de {SECTION_TYPES}, se recibió {value!r}"
            )
        return value


class SheetRow(Base):
    """A single financial concept (line item) within a section."""

    __tablename__ = "sheet_rows"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("sheet_sections.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # input | data | formula | subtotal | total | running_balance | label | separator
    row_type: Mapped[str] = mapped_column(String(20), nullable=False, default="input")
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # JSON: projection rule applied to all cells of this row unless overridden per-cell
    # e.g. {"type": "constant", "value": 700} or {"type": "previous_period"}
    default_projection_rule: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # JSON: how to read real data from the ledger. The engine itself never
    # reads this — it's a contract for the consuming app's own ledger bridge.
    # e.g. {"aggregate": "sum_amount", "filters": {"transaction_type": "expense", "category": "housing"}}
    ledger_mapping: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # positive | negative (used by subtotal/total rows to determine sign)
    sign: Mapped[str] = mapped_column(String(10), nullable=False, default="positive")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    section: Mapped["SheetSection"] = relationship(back_populates="rows")
    # No delete-orphan -- see CashflowSheet.sections' comment: a row's cells
    # (and their overrides/actual_entries) must survive the row being
    # deleted from a session, not cascade away with it.
    cells: Mapped[List["SheetCell"]] = relationship(
        back_populates="row", cascade="save-update, merge", passive_deletes=True
    )
    # Dependencies where this row is the target (consumes values from other rows)
    incoming_deps: Mapped[List["CellDependency"]] = relationship(
        foreign_keys="CellDependency.target_row_id",
        back_populates="target_row",
        cascade="all, delete-orphan",
    )
    # Dependencies where this row is the source (produces values for other rows)
    outgoing_deps: Mapped[List["CellDependency"]] = relationship(
        foreign_keys="CellDependency.source_row_id",
        back_populates="source_row",
        cascade="all, delete-orphan",
    )

    @validates("row_type")
    def _validate_row_type(self, key: str, value: str) -> str:
        if value not in ROW_TYPES:
            raise ValueError(f"row_type debe ser uno de {ROW_TYPES}, se recibió {value!r}")
        return value

    @validates("sign")
    def _validate_sign(self, key: str, value: str) -> str:
        if value not in ROW_SIGNS:
            raise ValueError(f"sign debe ser uno de {ROW_SIGNS}, se recibió {value!r}")
        return value


class SheetPeriod(Base):
    """A single time column (month) in the sheet."""

    __tablename__ = "sheet_periods"
    __table_args__ = (
        UniqueConstraint("sheet_id", "period_date", name="uq_sheet_period"),
        # close_period/reopen_period's ordering guards compare sort_order
        # with strict </> to decide "earlier"/"later" -- that only means
        # anything if sort_order is a genuine total order with no ties.
        # periods.extend_periods_backward can't itself produce a collision
        # (it always recomputes its pivot as the sheet's true minimum
        # sort_order and walks strictly below it -- see
        # test_extend_periods_backward_pivot_always_beats_an_out_of_band_
        # sort_order), but nothing else in the codebase enforced the
        # invariant at all before this constraint -- a future write path,
        # or a direct manual edit, could still violate it silently.
        UniqueConstraint("sheet_id", "sort_order", name="uq_period_sort_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sheet_id: Mapped[int] = mapped_column(ForeignKey("cashflow_sheets.id"), nullable=False, index=True)
    # Always the 1st day of the month
    period_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Display label, e.g. "Aug 2026"
    label: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)

    sheet: Mapped["CashflowSheet"] = relationship(back_populates="periods")
    # No delete-orphan -- see CashflowSheet.sections' comment: a period's
    # cells (and their overrides/actual_entries) must survive the period
    # being deleted from a session, not cascade away with it.
    cells: Mapped[List["SheetCell"]] = relationship(
        back_populates="period", cascade="save-update, merge", passive_deletes=True
    )


class SheetCell(Base):
    """Intersection of a row and a period. Created on demand."""

    __tablename__ = "sheet_cells"
    __table_args__ = (UniqueConstraint("row_id", "period_id", name="uq_cell"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    row_id: Mapped[int] = mapped_column(ForeignKey("sheet_rows.id"), nullable=False, index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("sheet_periods.id"), nullable=False, index=True)

    # RESERVED, not populated in V1 -- do not assume this is written. The
    # engine computes a projected value per (row, period) fresh on every
    # compute_sheet() call into an in-memory engine.CellResult, and never
    # persists it back onto this column (grep-confirmed: nothing in this
    # package ever assigns SheetCell.projected_value). Kept for a possible
    # future persisted-results cache.
    projected_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # Real layer (pulled from ledger — populated separately). These three
    # ARE live: written by wallet_movements.py, period_close.py, and the
    # CLI's own record commands.
    actual_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    accrued_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    paid_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # RESERVED, not populated in V1 -- same situation as projected_value
    # above: engine.CellResult computes pending_value/variance/
    # effective_source fresh every call and never writes them back here.
    pending_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    variance: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # Which source won: manual | rule | ledger | default | empty
    effective_source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    calculated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    row: Mapped["SheetRow"] = relationship(back_populates="cells")
    period: Mapped["SheetPeriod"] = relationship(back_populates="cells")
    # No delete-orphan on either audit-trail relationship below -- CellOverride
    # and CellActualEntry are both explicitly documented as append-only
    # ("Never updated; replaced by a new record" / "so a correction doesn't
    # erase the trail"). Deleting a SheetCell must never silently destroy
    # that trail; see CashflowSheet.sections' comment for the full rationale.
    overrides: Mapped[List["CellOverride"]] = relationship(
        back_populates="cell", cascade="save-update, merge", passive_deletes=True,
        # created_at alone doesn't break ties between two rows created in the
        # same instant (e.g. a tight seed/import loop) -- id is a stable
        # tiebreaker, and record_stack.py's push/pop algorithm depends on
        # this relationship being in true chronological order to be correct.
        order_by="CellOverride.created_at, CellOverride.id",
    )
    actual_entries: Mapped[List["CellActualEntry"]] = relationship(
        back_populates="cell", cascade="save-update, merge", passive_deletes=True,
        order_by="CellActualEntry.created_at, CellActualEntry.id",
    )
    computed_results: Mapped[List["ComputedResult"]] = relationship(
        back_populates="cell", cascade="all, delete-orphan"
    )


class CellOverride(Base):
    """Immutable record of a manual value or custom rule for a specific cell.

    Never updated; replaced by a new record. The previous record gets superseded_at set.
    Only the override with superseded_at = NULL is active.
    """

    __tablename__ = "cell_overrides"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    cell_id: Mapped[int] = mapped_column(ForeignKey("sheet_cells.id"), nullable=False, index=True)
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # manual_value | manual_rule | lock
    override_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # JSON rule used when override_type = manual_rule
    custom_rule: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Plain user id, not a ForeignKey — see CashflowSheet.user_id.
    created_by: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    # Set when this override is replaced by a newer one; NULL means active
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    cell: Mapped["SheetCell"] = relationship(back_populates="overrides")

    @validates("override_type")
    def _validate_override_type(self, key: str, value: str) -> str:
        if value not in OVERRIDE_TYPES:
            raise ValueError(
                f"override_type debe ser uno de {OVERRIDE_TYPES}, se recibió {value!r}"
            )
        return value


class CellActualEntry(Base):
    """Append-only audit log of writes to SheetCell's real layer
    (actual_value/accrued_value/paid_value).

    Unlike CellOverride, this is NOT something the engine resolves between
    competing sources — SheetCell.actual_value/accrued_value/paid_value are
    always the single source of truth the engine reads (see
    engine._real_fields). This table exists purely so a correction to a
    previously-recorded real value doesn't erase the trail of what it used
    to be. Every write inserts a new row with the FULL resulting state of
    the three fields (a snapshot, not a delta) — never updated.
    """

    __tablename__ = "cell_actual_entries"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    cell_id: Mapped[int] = mapped_column(ForeignKey("sheet_cells.id"), nullable=False, index=True)
    actual_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    accrued_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    paid_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # record | undo -- which record_stack.replay_record_stack used to tell
    # apart ONLY by sniffing whether `note` happened to start with a magic
    # string, with nothing reserving that prefix against a caller-supplied
    # note coincidentally starting the same way (see MIGRATIONS.md for the
    # backfill this column needed against pre-existing rows).
    entry_kind: Mapped[str] = mapped_column(String(10), nullable=False, default="record")
    # Plain user id, not a ForeignKey — see CashflowSheet.user_id.
    created_by: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)

    @validates("entry_kind")
    def _validate_entry_kind(self, key: str, value: str) -> str:
        if value not in ENTRY_KINDS:
            raise ValueError(f"entry_kind debe ser uno de {ENTRY_KINDS}, se recibió {value!r}")
        return value

    cell: Mapped["SheetCell"] = relationship(back_populates="actual_entries")


class CellDependency(Base):
    """Edge in the row-level dependency graph.

    Represents: target_row depends on source_row (possibly from a previous period).

    RESERVED, not populated in V1 -- grep-confirmed nothing in this package
    ever writes or reads a CellDependency row. engine.compute_sheet instead
    re-derives the dependency graph from SheetRow.default_projection_rule's
    JSON on every single call; nothing persists it relationally. Do not
    build a caller against this expecting live data -- despite what this
    docstring used to imply, nothing generates these rows automatically (or
    at all) today.
    """

    __tablename__ = "cell_dependencies"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Row that produces the value
    source_row_id: Mapped[int] = mapped_column(ForeignKey("sheet_rows.id"), nullable=False, index=True)
    # 0 = same period, -1 = previous period (cannot create intra-period cycles)
    source_period_offset: Mapped[int] = mapped_column(nullable=False, default=0)
    # Row that consumes the value
    target_row_id: Mapped[int] = mapped_column(ForeignKey("sheet_rows.id"), nullable=False, index=True)
    target_period_offset: Mapped[int] = mapped_column(nullable=False, default=0)
    # value | sum | balance
    dependency_type: Mapped[str] = mapped_column(String(20), nullable=False, default="value")

    source_row: Mapped["SheetRow"] = relationship(foreign_keys=[source_row_id], back_populates="outgoing_deps")
    target_row: Mapped["SheetRow"] = relationship(foreign_keys=[target_row_id], back_populates="incoming_deps")


class ComputedResult(Base):
    """Cache of the final effective value after the engine runs.

    Only one record per cell is current at a time (is_current = True).
    Not yet written by the engine in V1; reserved for future audit/history use.
    """

    __tablename__ = "computed_results"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    cell_id: Mapped[int] = mapped_column(ForeignKey("sheet_cells.id"), nullable=False, index=True)
    effective_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    # manual_override | custom_rule | default_rule | ledger | empty
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="empty")
    # Snapshot of the rule used during this computation
    rule_snapshot: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    cell: Mapped["SheetCell"] = relationship(back_populates="computed_results")


# ---------------------------------------------------------------------------
# Shared write helpers -- "get or create the SheetCell for (row_id,
# period_id)" and "supersede the active override before writing a new one"
# used to be reimplemented independently in 5-6 places across this package
# (seed.py, wallet_movements.py, period_close.py, creditcard_statements.py),
# with nothing enforcing they stayed in sync. One canonical copy here,
# reused everywhere, so the uq_cell / "only one active override" invariants
# below can't drift out from under a future change made in only one spot.
# ---------------------------------------------------------------------------

def get_or_create_cell(db: Session, row_id: int, period_id: int) -> "SheetCell":
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if cell is None:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    return cell


def supersede_active_override(db: Session, cell: "SheetCell") -> Optional["CellOverride"]:
    """Mark `cell`'s active override (superseded_at is None), if any, as
    superseded -- with no replacement written. After this call the cell has
    no active override at all, so the engine falls through to the row's own
    default_projection_rule again (see reopen_period, the one caller that
    wants exactly this: undo close_period's frozen ending-balance override
    without pinning the cell to some other fixed value). Returns the
    superseded override (or None if there wasn't one)."""
    previous = None
    for ov in cell.overrides:
        if ov.superseded_at is None:
            previous = ov
            break
    if previous is not None:
        previous.superseded_at = datetime.utcnow()
        db.flush()
    return previous


def supersede_and_write_override(
    db: Session,
    cell: "SheetCell",
    value: Optional[Decimal],
    *,
    created_by: int,
    override_type: str = "manual_value",
    custom_rule: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
) -> Tuple["CellOverride", Optional["CellOverride"]]:
    """Supersede `cell`'s active override (superseded_at is None), if any,
    then insert the new one. Returns (new_override, previous_override) --
    previous is None when the cell had none yet, letting a caller tell
    "replaced an existing override" apart from that case, e.g. for a status
    message. previous_override is the object itself (not just its value) so
    a caller can describe it correctly even for a manual_rule override,
    whose value column is always None."""
    previous = supersede_active_override(db, cell)
    new_override = CellOverride(
        cell_id=cell.id, value=value, override_type=override_type,
        custom_rule=custom_rule, note=note, created_by=created_by,
    )
    db.add(new_override)
    db.flush()
    return new_override, previous
