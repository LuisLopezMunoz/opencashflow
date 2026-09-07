"""Tests for opencashflow.enums (the canonical closed-value-set registry)
and for the @validates guards on models.py/credit_card.py that now enforce
it at the ORM layer -- the boundary that used to have no protection at all
(a typo'd sign/row_type/section_type/override_type/status/source/line_type
used to pass straight into the database and only surface, silently wrong,
deep inside engine.py's sign resolution).
"""
from datetime import date, datetime
from typing import get_args

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.credit_card import CreditCard, CreditCardStatement, CreditCardStatementLine
from opencashflow.enums import (
    CREDIT_CARD_STATUSES,
    CreditCardStatus,
    ENTRY_KINDS,
    EntryKind,
    OVERRIDE_TYPES,
    OverrideType,
    ROW_SIGNS,
    ROW_TYPES,
    RowSign,
    RowType,
    SECTION_TYPES,
    STATEMENT_LINE_TYPES,
    STATEMENT_SOURCES,
    SectionType,
    StatementLineType,
    StatementSource,
)
from opencashflow.models import Base, CashflowSheet, CellActualEntry, CellOverride, SheetCell, SheetRow, SheetSection

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# "No drift" -- each runtime tuple and its matching Literal alias must name
# exactly the same values. This is what actually keeps them in sync, instead
# of relying on eyeballing two hand-written lists a few lines apart.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tuple_values, literal_type", [
    (ROW_SIGNS, RowSign),
    (ROW_TYPES, RowType),
    (SECTION_TYPES, SectionType),
    (OVERRIDE_TYPES, OverrideType),
    (CREDIT_CARD_STATUSES, CreditCardStatus),
    (STATEMENT_SOURCES, StatementSource),
    (STATEMENT_LINE_TYPES, StatementLineType),
    (ENTRY_KINDS, EntryKind),
])
def test_tuple_and_literal_alias_do_not_drift(tuple_values, literal_type):
    assert set(get_args(literal_type)) == set(tuple_values)


def test_aggregate_row_types_excludes_only_the_real_leaf_types():
    from opencashflow.enums import AGGREGATE_ROW_TYPES
    assert AGGREGATE_ROW_TYPES == frozenset(ROW_TYPES) - {"input", "data"}
    assert "formula" in AGGREGATE_ROW_TYPES


# ---------------------------------------------------------------------------
# models.py @validates guards
# ---------------------------------------------------------------------------

def _sheet(db) -> CashflowSheet:
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Enum Test Sheet", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    return sheet


def test_sheet_row_rejects_bad_sign(db):
    section = SheetSection(sheet_id=_sheet(db).id, name="S", section_type="custom")
    db.add(section)
    db.flush()
    with pytest.raises(ValueError, match="sign"):
        SheetRow(section_id=section.id, name="A", sign="Positive")


def test_sheet_row_rejects_bad_row_type(db):
    section = SheetSection(sheet_id=_sheet(db).id, name="S", section_type="custom")
    db.add(section)
    db.flush()
    with pytest.raises(ValueError, match="row_type"):
        SheetRow(section_id=section.id, name="A", row_type="subtotall")


def test_sheet_section_rejects_bad_section_type(db):
    with pytest.raises(ValueError, match="section_type"):
        SheetSection(sheet_id=_sheet(db).id, name="S", section_type="custome")


def test_cell_override_rejects_bad_override_type(db):
    sheet = _sheet(db)
    section = SheetSection(sheet_id=sheet.id, name="S", section_type="custom")
    db.add(section)
    db.flush()
    row = SheetRow(section_id=section.id, name="A")
    db.add(row)
    db.flush()
    cell = SheetCell(row_id=row.id, period_id=1)
    db.add(cell)
    db.flush()
    with pytest.raises(ValueError, match="override_type"):
        CellOverride(cell_id=cell.id, value=1, override_type="manual", created_by=TEST_USER_ID)


# ---------------------------------------------------------------------------
# credit_card.py @validates guards
# ---------------------------------------------------------------------------

def test_credit_card_rejects_bad_status():
    with pytest.raises(ValueError, match="status"):
        CreditCard(user_id=TEST_USER_ID, name="X", credit_limit=1.0, status="activ")


def test_credit_card_statement_rejects_bad_source():
    with pytest.raises(ValueError, match="source"):
        CreditCardStatement(
            credit_card_id=1, billing_period=date(2026, 1, 1), new_balance=1, total_payment_due=1,
            closing_date=date(2026, 1, 1), due_date=date(2026, 1, 1), source="pdf",
        )


def test_credit_card_statement_line_rejects_bad_line_type():
    with pytest.raises(ValueError, match="line_type"):
        CreditCardStatementLine(
            statement_id=1, line_type="refund", description="x", transaction_date=date(2026, 1, 1), amount=1,
        )


def test_cell_actual_entry_rejects_bad_entry_kind():
    with pytest.raises(ValueError, match="entry_kind"):
        CellActualEntry(cell_id=1, entry_kind="reverted", created_by=TEST_USER_ID)
