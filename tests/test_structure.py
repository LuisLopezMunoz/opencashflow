"""Tests for opencashflow.structure -- add_section/add_row/edit_row (cli.py
aliases these as _do_add_section/_do_add_row/_do_edit_row) and
validate_rule, the check every rule passes before it's stored.

Direct ORM construction, same style as test_overrides.py.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow import cli
from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CashflowSheet, SheetPeriod, SheetRow
from opencashflow.overrides import set_override
from opencashflow.structure import add_row, add_section, edit_row, rule_row_refs, validate_rule

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_sheet(db, name="Planilla"):
    s = CashflowSheet(user_id=TEST_USER_ID, name=name, currency="CLP",
                       horizon_months=2, base_period=datetime(2026, 1, 1))
    db.add(s)
    db.flush()
    db.add_all([
        SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 1, 1), label="Jan 2026", sort_order=0),
        SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 2, 1), label="Feb 2026", sort_order=1),
    ])
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def sheet(db):
    return _make_sheet(db)


@pytest.fixture()
def section(db, sheet):
    return add_section(db, sheet, "Ingresos", "income")


@pytest.fixture()
def sueldo(db, section):
    return add_row(db, section, "Sueldo", rule={"type": "constant", "value": 1000})


def _row_count(db):
    return db.query(SheetRow).count()


# ---------------------------------------------------------------------------
# The CLI's helpers ARE these functions -- one implementation.
# ---------------------------------------------------------------------------

def test_cli_helpers_are_the_structure_functions():
    assert cli._do_add_section is add_section
    assert cli._do_add_row is add_row
    assert cli._do_edit_row is edit_row


# ---------------------------------------------------------------------------
# add_section / add_row
# ---------------------------------------------------------------------------

def test_add_section_appends_by_default_and_respects_an_explicit_order(db, sheet):
    first = add_section(db, sheet, "Primera")
    second = add_section(db, sheet, "Segunda")
    explicit = add_section(db, sheet, "Fija", sort_order=7)
    assert (first.sort_order, second.sort_order, explicit.sort_order) == (0, 1, 7)


def test_add_section_stores_its_display_fields(db, sheet):
    s = add_section(db, sheet, "Gastos", "expense", is_collapsible=False, color_hex="#ff0000")
    assert (s.is_collapsible, s.color_hex) == (False, "#ff0000")


def test_add_section_rejects_an_unknown_type(db, sheet):
    with pytest.raises(ValueError, match="section_type"):
        add_section(db, sheet, "Rara", "bogus")


def test_add_row_appends_within_its_section(db, section, sueldo):
    other = add_row(db, section, "Bono")
    assert other.sort_order == sueldo.sort_order + 1


def test_add_row_stores_every_field(db, section):
    row = add_row(
        db, section, "Arriendo", "input", "negative", None, 3,
        is_visible=False, ledger_mapping={"category": "housing"}, notes="contrato 2026",
    )
    assert (row.sign, row.sort_order, row.is_visible) == ("negative", 3, False)
    assert row.ledger_mapping == {"category": "housing"}
    assert row.notes == "contrato 2026"


def test_add_row_accepts_a_reference_to_a_row_of_its_own_sheet(db, section, sueldo):
    total = add_row(db, section, "Total", "total", rule={"type": "sum_rows", "row_ids": [sueldo.id]})
    assert total.default_projection_rule == {"type": "sum_rows", "row_ids": [sueldo.id]}


def test_add_row_rejects_a_reference_to_another_sheets_row_and_writes_nothing(db, section):
    other_sheet = _make_sheet(db, "Otra")
    foreign = add_row(db, add_section(db, other_sheet, "X"), "Ajena")
    before = _row_count(db)
    with pytest.raises(ValueError, match="no existen en la planilla"):
        add_row(db, section, "Total", "total", rule={"type": "sum_rows", "row_ids": [foreign.id]})
    assert _row_count(db) == before


def test_add_row_still_allows_an_unknown_rule_type(db, section):
    # The engine reports it per cell (unsupported_rule:<type>) -- visible, not silent.
    row = add_row(db, section, "Futura", rule={"type": "something_new"})
    assert row.default_projection_rule == {"type": "something_new"}


# ---------------------------------------------------------------------------
# validate_rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule", [
    {"type": "sum_rows", "row_ids": [999]},
    {"type": "percent_of_row", "row_id": 999, "percent": 10},
    {"type": "previous_period", "row_id": 999},
    {"type": "carry_forward", "base_rule": {"type": "sum_rows", "row_ids": [999]}},
])
def test_validate_rule_rejects_a_missing_reference_in_every_rule_type(db, sheet, rule):
    with pytest.raises(ValueError, match=r"\[999\]"):
        validate_rule(db, sheet.id, rule)


@pytest.mark.parametrize("rule", [
    {"type": "sum_rows", "row_ids": ["1"]},
    {"type": "sum_rows", "row_ids": [True]},
    {"type": "sum_rows", "row_ids": "1"},
    {"type": "percent_of_row", "row_id": 1.5, "percent": 10},
])
def test_validate_rule_rejects_non_integer_ids(db, sheet, rule):
    with pytest.raises(ValueError, match="no son enteros"):
        validate_rule(db, sheet.id, rule)


def test_validate_rule_rejects_a_non_dict_carry_forward_base_rule(db, sheet):
    with pytest.raises(ValueError, match="objeto JSON"):
        validate_rule(db, sheet.id, {"type": "carry_forward", "base_rule": "constant"})


def test_a_non_dict_base_rule_really_would_crash_the_whole_sheet(db, section, sueldo):
    # Why validate_rule rejects it: stored anyway (straight through the ORM),
    # compute_sheet raises instead of isolating the bad cell.
    sueldo.default_projection_rule = {"type": "carry_forward", "base_rule": "constant"}
    db.commit()
    with pytest.raises(AttributeError):
        compute_sheet(section.sheet_id, db)


def test_validate_rule_accepts_none_and_reference_free_rules(db, sheet):
    validate_rule(db, sheet.id, None)
    validate_rule(db, sheet.id, {"type": "constant", "value": 5})
    validate_rule(db, sheet.id, {"type": "previous_period"})
    validate_rule(db, sheet.id, {"type": "carry_forward"})


def test_rule_row_refs_covers_every_referencing_rule_type():
    assert rule_row_refs({"type": "sum_rows", "row_ids": [1, 2]}) == [1, 2]
    assert rule_row_refs({"type": "percent_of_row", "row_id": 3, "percent": 5}) == [3]
    assert rule_row_refs({"type": "previous_period", "row_id": 4}) == [4]
    assert rule_row_refs({"type": "previous_period"}) == []
    assert rule_row_refs({"type": "carry_forward", "base_rule": {"type": "sum_rows", "row_ids": [5]}}) == [5]
    assert rule_row_refs({"type": "constant", "value": 1}) == []
    assert rule_row_refs(None) == []
    assert rule_row_refs("not-a-rule") == []


# ---------------------------------------------------------------------------
# edit_row
# ---------------------------------------------------------------------------

def test_edit_row_reports_and_applies_only_real_changes(db, sueldo):
    changes = edit_row(db, sueldo, name="Sueldo base", sign=sueldo.sign)
    assert changes == {"name": ("Sueldo", "Sueldo base")}
    assert sueldo.name == "Sueldo base"


def test_edit_row_with_nothing_passed_changes_nothing(db, sueldo):
    assert edit_row(db, sueldo) == {}


def test_edit_row_rule_none_clears_the_rule(db, sueldo):
    changes = edit_row(db, sueldo, rule=None)
    assert changes == {"rule": ({"type": "constant", "value": 1000}, None)}
    assert sueldo.default_projection_rule is None


def test_edit_row_covers_visibility_ledger_mapping_and_notes(db, sueldo):
    changes = edit_row(db, sueldo, is_visible=False, ledger_mapping={"k": "v"}, notes="x")
    assert set(changes) == {"is_visible", "ledger_mapping", "notes"}
    assert edit_row(db, sueldo, notes=None) == {"notes": ("x", None)}


@pytest.mark.parametrize("kwargs", [
    {"name": "Cambiado", "sign": "sideways"},
    {"name": "Cambiado", "row_type": "bogus"},
    {"name": "Cambiado", "rule": {"type": "sum_rows", "row_ids": [999]}},
])
def test_edit_row_validates_everything_before_touching_the_row(db, sueldo, kwargs):
    with pytest.raises(ValueError):
        edit_row(db, sueldo, **kwargs)
    assert sueldo.name == "Sueldo"
    assert not db.dirty


# ---------------------------------------------------------------------------
# The same check guards a manual_rule override, and doctor uses the same
# reference walk (so it now also sees inside a carry_forward's base_rule).
# ---------------------------------------------------------------------------

def test_set_override_rejects_a_rule_referencing_a_row_outside_the_sheet(db, sheet, sueldo):
    with pytest.raises(ValueError, match="no existen en la planilla"):
        set_override(db, sheet.id, sueldo, [sheet.periods[0]],
                     rule={"type": "sum_rows", "row_ids": [999]}, created_by=TEST_USER_ID)


def test_set_override_still_accepts_a_valid_rule(db, sheet, section, sueldo):
    bono = add_row(db, section, "Bono", rule={"type": "constant", "value": 5})
    results = set_override(db, sheet.id, sueldo, [sheet.periods[0]],
                           rule={"type": "sum_rows", "row_ids": [bono.id]}, created_by=TEST_USER_ID)
    assert results[0].override.override_type == "manual_rule"


def test_doctor_flags_a_missing_reference_inside_a_carry_forward(db, section, sueldo):
    sueldo.default_projection_rule = {
        "type": "carry_forward", "base_rule": {"type": "sum_rows", "row_ids": [999]},
    }
    db.commit()
    issues = cli._diagnose(compute_sheet(section.sheet_id, db))
    assert any("999" in issue for issue in issues)


def test_doctor_survives_a_malformed_stored_reference(db, section, sueldo):
    # An unhashable ref must not crash doctor's own membership check.
    sueldo.default_projection_rule = {"type": "sum_rows", "row_ids": {"a": 1}}
    db.commit()
    issues = cli._diagnose(compute_sheet(section.sheet_id, db))
    assert any("Sueldo" in issue for issue in issues)


def test_override_values_unaffected_by_validation(db, sheet, sueldo):
    results = set_override(db, sheet.id, sueldo, [sheet.periods[0]], value=Decimal("7"), created_by=TEST_USER_ID)
    assert results[0].override.value == Decimal("7")
