"""Unit tests for opencashflow.engine's sign-resolution trio:
row_sign_multiplier, build_sum_rows_hierarchy, effective_sign_to_top.

These generalize the engine's own per-level sum_rows sign handling into a
full path-to-root walk, answering "does a one-unit change in this row make
the topmost aggregate go up or down" -- independent of compute_sheet()
itself, no HTTP, no auth.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import build_sum_rows_hierarchy, effective_sign_to_top, row_sign_multiplier
from opencashflow.models import Base, CashflowSheet, SheetRow, SheetSection

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


def _sheet(db) -> CashflowSheet:
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Sign Resolution Test Sheet", currency="CLP",
                           horizon_months=1, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    return sheet


def _section(db, sheet, name="Section") -> SheetSection:
    section = SheetSection(sheet_id=sheet.id, name=name, section_type="custom")
    db.add(section)
    db.flush()
    return section


def _row(db, section, name, sign="positive", rule=None) -> SheetRow:
    row = SheetRow(section_id=section.id, name=name, sign=sign, default_projection_rule=rule)
    db.add(row)
    db.flush()
    return row


def test_row_sign_multiplier():
    positive = SheetRow(sign="positive")
    negative = SheetRow(sign="negative")
    assert row_sign_multiplier(positive) == 1
    assert row_sign_multiplier(negative) == -1


def test_row_with_no_parent_returns_none_not_zero(db):
    sheet = _sheet(db)
    section = _section(db, sheet)
    orphan = _row(db, section, "Orphan")
    db.commit()

    rows_by_id, child_to_parent = build_sum_rows_hierarchy(db, sheet.id)

    assert effective_sign_to_top(orphan.id, rows_by_id, child_to_parent) is None


def test_multi_hop_chain_multiplies_sign_at_each_level(db):
    sheet = _sheet(db)
    section = _section(db, sheet)
    leaf = _row(db, section, "Gasto", sign="positive")
    subtotal = _row(db, section, "Total Gastos", sign="negative",
                     rule={"type": "sum_rows", "row_ids": [leaf.id]})
    total = _row(db, section, "Flujo Neto", sign="positive",
                 rule={"type": "sum_rows", "row_ids": [subtotal.id]})
    db.commit()

    rows_by_id, child_to_parent = build_sum_rows_hierarchy(db, sheet.id)

    # leaf: multiplies its OWN sign (+1) then subtotal's sign (-1) on the way
    # up => -1 (an increase in the expense leaf decreases Flujo Neto).
    assert effective_sign_to_top(leaf.id, rows_by_id, child_to_parent) == -1
    # subtotal: one hop to its parent (total), so its effective sign IS its
    # own sign (-1) -- total's own sign is excluded (nothing sums total).
    assert effective_sign_to_top(subtotal.id, rows_by_id, child_to_parent) == -1
    # The topmost row (nobody sums it) has no parent either.
    assert effective_sign_to_top(total.id, rows_by_id, child_to_parent) is None


def test_ambiguous_parent_raises(db):
    sheet = _sheet(db)
    section = _section(db, sheet)
    leaf = _row(db, section, "Shared Leaf")
    db.commit()
    parent_a = _row(db, section, "Parent A", rule={"type": "sum_rows", "row_ids": [leaf.id]})
    parent_b = _row(db, section, "Parent B", rule={"type": "sum_rows", "row_ids": [leaf.id]})
    db.commit()

    with pytest.raises(ValueError, match="more than one row"):
        build_sum_rows_hierarchy(db, sheet.id)


def test_cycle_guard_raises_past_20_hops(db):
    sheet = _sheet(db)
    section = _section(db, sheet)
    rows = [_row(db, section, f"Row {i}") for i in range(25)]
    db.commit()
    # Chain each row into the next, forming a 25-hop path (no real cycle,
    # just deep enough to trip the defensive hop-count guard).
    for i in range(len(rows) - 1):
        rows[i + 1].default_projection_rule = {"type": "sum_rows", "row_ids": [rows[i].id]}
    db.commit()

    rows_by_id, child_to_parent = build_sum_rows_hierarchy(db, sheet.id)

    with pytest.raises(ValueError, match="exceeds 20 levels"):
        effective_sign_to_top(rows[0].id, rows_by_id, child_to_parent)
