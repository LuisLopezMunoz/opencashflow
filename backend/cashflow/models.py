from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.database import Base


class CashflowSheet(Base):
    """Root spreadsheet. Belongs to one user, defines the time horizon and base currency."""

    __tablename__ = "cashflow_sheets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(150), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    horizon_months = Column(Integer, nullable=False, default=12)
    # First day of the first period (always day=1 of a month)
    base_period = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="cashflow_sheets")
    sections = relationship(
        "SheetSection", back_populates="sheet", cascade="all, delete-orphan", order_by="SheetSection.sort_order"
    )
    periods = relationship(
        "SheetPeriod", back_populates="sheet", cascade="all, delete-orphan", order_by="SheetPeriod.sort_order"
    )


class SheetSection(Base):
    """Visual grouping of rows within a sheet (e.g. 'Income', 'Fixed Expenses')."""

    __tablename__ = "sheet_sections"

    id = Column(Integer, primary_key=True, index=True)
    sheet_id = Column(Integer, ForeignKey("cashflow_sheets.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    # income | expense | financing | balance | custom
    section_type = Column(String(20), nullable=False, default="custom")
    sort_order = Column(Integer, nullable=False, default=0)
    is_collapsible = Column(Boolean, nullable=False, default=True)
    color_hex = Column(String(7), nullable=True)

    sheet = relationship("CashflowSheet", back_populates="sections")
    rows = relationship(
        "SheetRow", back_populates="section", cascade="all, delete-orphan", order_by="SheetRow.sort_order"
    )


class SheetRow(Base):
    """A single financial concept (line item) within a section."""

    __tablename__ = "sheet_rows"

    id = Column(Integer, primary_key=True, index=True)
    section_id = Column(Integer, ForeignKey("sheet_sections.id"), nullable=False, index=True)
    name = Column(String(150), nullable=False)
    # input | data | formula | subtotal | total | running_balance | label | separator
    row_type = Column(String(20), nullable=False, default="input")
    sort_order = Column(Integer, nullable=False, default=0)
    is_visible = Column(Boolean, nullable=False, default=True)
    # JSON: projection rule applied to all cells of this row unless overridden per-cell
    # e.g. {"type": "constant", "value": 700} or {"type": "previous_period"}
    default_projection_rule = Column(JSON, nullable=True)
    # JSON: how to read real data from the ledger
    # e.g. {"aggregate": "sum_amount", "filters": {"transaction_type": "expense", "category": "housing"}}
    ledger_mapping = Column(JSON, nullable=True)
    # positive | negative (used by subtotal/total rows to determine sign)
    sign = Column(String(10), nullable=False, default="positive")
    notes = Column(Text, nullable=True)

    section = relationship("SheetSection", back_populates="rows")
    cells = relationship("SheetCell", back_populates="row", cascade="all, delete-orphan")
    # Dependencies where this row is the target (consumes values from other rows)
    incoming_deps = relationship(
        "CellDependency",
        foreign_keys="CellDependency.target_row_id",
        back_populates="target_row",
        cascade="all, delete-orphan",
    )
    # Dependencies where this row is the source (produces values for other rows)
    outgoing_deps = relationship(
        "CellDependency",
        foreign_keys="CellDependency.source_row_id",
        back_populates="source_row",
        cascade="all, delete-orphan",
    )


class SheetPeriod(Base):
    """A single time column (month) in the sheet."""

    __tablename__ = "sheet_periods"
    __table_args__ = (UniqueConstraint("sheet_id", "period_date", name="uq_sheet_period"),)

    id = Column(Integer, primary_key=True, index=True)
    sheet_id = Column(Integer, ForeignKey("cashflow_sheets.id"), nullable=False, index=True)
    # Always the 1st day of the month
    period_date = Column(DateTime, nullable=False)
    # Display label, e.g. "Aug 2026"
    label = Column(String(30), nullable=True)
    is_closed = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)

    sheet = relationship("CashflowSheet", back_populates="periods")
    cells = relationship("SheetCell", back_populates="period", cascade="all, delete-orphan")


class SheetCell(Base):
    """Intersection of a row and a period. Created on demand."""

    __tablename__ = "sheet_cells"
    __table_args__ = (UniqueConstraint("row_id", "period_id", name="uq_cell"),)

    id = Column(Integer, primary_key=True, index=True)
    row_id = Column(Integer, ForeignKey("sheet_rows.id"), nullable=False, index=True)
    period_id = Column(Integer, ForeignKey("sheet_periods.id"), nullable=False, index=True)

    # Projection layer (derived from rules + overrides)
    projected_value = Column(Numeric(14, 2), nullable=True)
    # Real layer (pulled from ledger — populated separately)
    actual_value = Column(Numeric(14, 2), nullable=True)
    accrued_value = Column(Numeric(14, 2), nullable=True)
    paid_value = Column(Numeric(14, 2), nullable=True)
    # Computed fields (calculated by the engine, stored for display)
    pending_value = Column(Numeric(14, 2), nullable=True)
    variance = Column(Numeric(14, 2), nullable=True)
    # Which source won: manual | rule | ledger | default | empty
    effective_source = Column(String(20), nullable=True)
    is_locked = Column(Boolean, nullable=False, default=False)
    calculated_at = Column(DateTime, nullable=True)

    row = relationship("SheetRow", back_populates="cells")
    period = relationship("SheetPeriod", back_populates="cells")
    overrides = relationship(
        "CellOverride", back_populates="cell", cascade="all, delete-orphan", order_by="CellOverride.created_at"
    )
    computed_results = relationship(
        "ComputedResult", back_populates="cell", cascade="all, delete-orphan"
    )


class CellOverride(Base):
    """Immutable record of a manual value or custom rule for a specific cell.

    Never updated; replaced by a new record. The previous record gets superseded_at set.
    Only the override with superseded_at = NULL is active.
    """

    __tablename__ = "cell_overrides"

    id = Column(Integer, primary_key=True, index=True)
    cell_id = Column(Integer, ForeignKey("sheet_cells.id"), nullable=False, index=True)
    value = Column(Numeric(14, 2), nullable=True)
    # manual_value | manual_rule | lock
    override_type = Column(String(20), nullable=False)
    # JSON rule used when override_type = manual_rule
    custom_rule = Column(JSON, nullable=True)
    note = Column(String(255), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Set when this override is replaced by a newer one; NULL means active
    superseded_at = Column(DateTime, nullable=True)

    cell = relationship("SheetCell", back_populates="overrides")
    creator = relationship("User")


class CellDependency(Base):
    """Edge in the row-level dependency graph.

    Represents: target_row depends on source_row (possibly from a previous period).
    Generated automatically when a projection rule is assigned or modified.
    """

    __tablename__ = "cell_dependencies"

    id = Column(Integer, primary_key=True, index=True)
    # Row that produces the value
    source_row_id = Column(Integer, ForeignKey("sheet_rows.id"), nullable=False, index=True)
    # 0 = same period, -1 = previous period (cannot create intra-period cycles)
    source_period_offset = Column(Integer, nullable=False, default=0)
    # Row that consumes the value
    target_row_id = Column(Integer, ForeignKey("sheet_rows.id"), nullable=False, index=True)
    target_period_offset = Column(Integer, nullable=False, default=0)
    # value | sum | balance
    dependency_type = Column(String(20), nullable=False, default="value")

    source_row = relationship("SheetRow", foreign_keys=[source_row_id], back_populates="outgoing_deps")
    target_row = relationship("SheetRow", foreign_keys=[target_row_id], back_populates="incoming_deps")


class ComputedResult(Base):
    """Cache of the final effective value after the engine runs.

    Only one record per cell is current at a time (is_current = True).
    Not yet written by the engine in V1; reserved for future audit/history use.
    """

    __tablename__ = "computed_results"

    id = Column(Integer, primary_key=True, index=True)
    cell_id = Column(Integer, ForeignKey("sheet_cells.id"), nullable=False, index=True)
    effective_value = Column(Numeric(14, 2), nullable=True)
    # manual_override | custom_rule | default_rule | ledger | empty
    source = Column(String(20), nullable=False, default="empty")
    # Snapshot of the rule used during this computation
    rule_snapshot = Column(JSON, nullable=True)
    computed_at = Column(DateTime, default=datetime.utcnow)
    is_current = Column(Boolean, nullable=False, default=True)

    cell = relationship("SheetCell", back_populates="computed_results")
