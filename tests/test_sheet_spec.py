"""Tests for opencashflow.sheet_spec: the Pydantic rule/sheet models, the
two-pass import_sheet_spec/export_sheet_spec loader, and file I/O
(load_sheet_spec/dump_sheet_spec/serialize_sheet_spec).
"""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pydantic
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import compute_sheet
from opencashflow.models import Base
from opencashflow.seed import seed_sheet
from opencashflow.sheet_spec import (
    RULE_SPEC_CLASSES,
    CarryForwardRuleSpec,
    ConstantRuleSpec,
    PercentOfRowRuleSpec,
    PreviousPeriodRuleSpec,
    RollingAverageRuleSpec,
    SheetMetaSpec,
    SheetSpec,
    SumRowsRuleSpec,
    dump_sheet_spec,
    export_sheet_spec,
    find_unseeded_running_balance_rows,
    import_sheet_spec,
    load_sheet_spec,
    serialize_sheet_spec,
)

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Parity across the three hand-rolled rule-type dispatches (see
# RULE_SPEC_CLASSES's own comment in sheet_spec.py) -- one minimal instance
# per rule class, run through all three, confirming none falls through to
# its "unrecognized type" branch. A rule type added to one dispatch but not
# the others would fail here instead of only at runtime on a specific cell.
# ---------------------------------------------------------------------------

_MINIMAL_RULE_INSTANCES = {
    ConstantRuleSpec: ConstantRuleSpec(value=1),
    PreviousPeriodRuleSpec: PreviousPeriodRuleSpec(row_id="X"),
    SumRowsRuleSpec: SumRowsRuleSpec(row_ids=["X"]),
    PercentOfRowRuleSpec: PercentOfRowRuleSpec(row_id="X", percent=10),
    RollingAverageRuleSpec: RollingAverageRuleSpec(n=3),
    CarryForwardRuleSpec: CarryForwardRuleSpec(base_rule=ConstantRuleSpec(value=1)),
}


def test_every_rule_spec_class_has_a_minimal_instance_for_the_parity_test():
    # Guards the test fixture above itself against silently going stale if
    # RULE_SPEC_CLASSES ever gains a member nobody added a case for here.
    assert set(_MINIMAL_RULE_INSTANCES) == set(RULE_SPEC_CLASSES)


@pytest.mark.parametrize("rule_cls", RULE_SPEC_CLASSES)
def test_rule_dispatch_parity_forward_and_referenced_names(rule_cls):
    from opencashflow.sheet_spec import _referenced_names, _resolve_rule_spec

    rule = _MINIMAL_RULE_INSTANCES[rule_cls]
    name_to_row = {"X": SimpleNamespace(id=1)}

    resolved = _resolve_rule_spec(rule, name_to_row, "TestRow")
    assert resolved["type"] == rule.type

    # Must not raise for a resolvable reference (name_to_row has "X").
    for name in _referenced_names(rule):
        assert name in name_to_row


@pytest.mark.parametrize("rule_cls", RULE_SPEC_CLASSES)
def test_rule_dispatch_parity_reverse(rule_cls):
    from opencashflow.sheet_spec import _reverse_resolve_rule, _resolve_rule_spec

    rule = _MINIMAL_RULE_INSTANCES[rule_cls]
    name_to_row = {"X": SimpleNamespace(id=1)}
    id_to_name = {1: "X"}

    resolved = _resolve_rule_spec(rule, name_to_row, "TestRow")
    reversed_spec = _reverse_resolve_rule(resolved, id_to_name, "TestRow")
    assert isinstance(reversed_spec, rule_cls)


def _spec(**overrides):
    base = {
        "sheet": {"name": "Test", "currency": "CLP", "horizon_months": 3, "base_period": "2026-01"},
        "sections": [],
    }
    base.update(overrides)
    return SheetSpec.model_validate(base)


# ---------------------------------------------------------------------------
# One test per rule type
# ---------------------------------------------------------------------------

def test_constant_rule(db):
    spec = _spec(sections=[{
        "name": "Ingresos", "rows": [{"name": "Sueldo", "rule": {"type": "constant", "value": 100}}],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    values = result["sections"][0]["rows"][0]["cells"]
    assert [c.projected_value for c in values] == [100, 100, 100]


def test_previous_period_with_explicit_row_id(db):
    spec = _spec(sections=[{
        "name": "Saldo", "rows": [
            {"name": "A", "rule": {"type": "constant", "value": 10}},
            {"name": "B", "rule": {"type": "previous_period", "row_id": "A"}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    b_cells = result["sections"][0]["rows"][1]["cells"]
    assert [c.projected_value for c in b_cells] == [None, 10, 10]


def test_previous_period_without_row_id_defaults_to_self(db):
    spec = _spec(sections=[{
        "name": "Saldo", "rows": [{"name": "A", "rule": {"type": "previous_period"}}],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][0]["cells"]
    assert [c.projected_value for c in cells] == [None, None, None]


def test_sum_rows(db):
    spec = _spec(sections=[{
        "name": "Ingresos", "rows": [
            {"name": "A", "rule": {"type": "constant", "value": 10}},
            {"name": "B", "rule": {"type": "constant", "value": 20}},
            {"name": "Total", "rule": {"type": "sum_rows", "row_ids": ["A", "B"]}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    total_cells = result["sections"][0]["rows"][2]["cells"]
    assert [c.projected_value for c in total_cells] == [30, 30, 30]


def test_sum_rows_empty_list_is_valid(db):
    spec = _spec(sections=[{
        "name": "Financiamiento", "rows": [{"name": "Total", "rule": {"type": "sum_rows", "row_ids": []}}],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][0]["cells"]
    assert [c.projected_value for c in cells] == [None, None, None]


def test_percent_of_row(db):
    spec = _spec(sections=[{
        "name": "Ingresos", "rows": [
            {"name": "Sueldo", "rule": {"type": "constant", "value": 1000}},
            {"name": "Ahorro", "rule": {"type": "percent_of_row", "row_id": "Sueldo", "percent": 10}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][1]["cells"]
    assert [c.projected_value for c in cells] == [100, 100, 100]


def test_rolling_average(db):
    spec = _spec(horizon_months=4, sections=[{
        "name": "Gastos", "rows": [{"name": "Variable", "rule": {"type": "rolling_average", "n": 2}}],
    }])
    # rolling_average has no seed data to average over in a fresh sheet --
    # confirm it resolves to empty rather than erroring, same as the engine's
    # own "no data" convention.
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][0]["cells"]
    assert all(c.projected_value is None for c in cells)


def test_carry_forward_uses_base_rule(db):
    spec = _spec(sections=[{
        "name": "Gastos", "rows": [
            {"name": "Arrastre", "rule": {"type": "carry_forward", "base_rule": {"type": "constant", "value": 50}}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][0]["cells"]
    assert [c.projected_value for c in cells] == [50, 50, 50]


def test_carry_forward_base_rule_can_reference_another_row(db):
    spec = _spec(sections=[{
        "name": "Gastos", "rows": [
            {"name": "Base", "rule": {"type": "constant", "value": 7}},
            {"name": "Arrastre", "rule": {
                "type": "carry_forward", "base_rule": {"type": "sum_rows", "row_ids": ["Base"]},
            }},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][1]["cells"]
    assert [c.projected_value for c in cells] == [7, 7, 7]


# ---------------------------------------------------------------------------
# Errors: duplicate names, unresolvable references
# ---------------------------------------------------------------------------

def test_duplicate_row_name_across_sections_raises(db):
    spec = _spec(sections=[
        {"name": "Sec1", "rows": [{"name": "Igual"}]},
        {"name": "Sec2", "rows": [{"name": "Igual"}]},
    ])
    with pytest.raises(ValueError, match="Igual"):
        import_sheet_spec(db, spec, TEST_USER_ID)


def test_unresolvable_row_id_reference_names_both_rows(db):
    spec = _spec(sections=[{
        "name": "Ingresos", "rows": [
            {"name": "Ahorro", "rule": {"type": "percent_of_row", "row_id": "NoExiste", "percent": 10}},
        ],
    }])
    with pytest.raises(ValueError, match="Ahorro"):
        import_sheet_spec(db, spec, TEST_USER_ID)
    with pytest.raises(ValueError, match="NoExiste"):
        import_sheet_spec(db, spec, TEST_USER_ID)


def test_unresolvable_reference_nested_inside_carry_forward_base_rule(db):
    spec = _spec(sections=[{
        "name": "Gastos", "rows": [
            {"name": "Arrastre", "rule": {
                "type": "carry_forward",
                "base_rule": {"type": "percent_of_row", "row_id": "Fantasma", "percent": 5},
            }},
        ],
    }])
    with pytest.raises(ValueError, match="Fantasma"):
        import_sheet_spec(db, spec, TEST_USER_ID)


def test_failed_import_leaves_no_orphaned_sheet_in_the_session(db):
    """import_sheet_spec used to db.add()/db.flush() the CashflowSheet (and
    every section/row) BEFORE the duplicate-name/unresolvable-reference
    checks that can raise -- a bad spec left a half-built, queryable
    CashflowSheet in the session, since the ValueError path never called
    db.rollback(). Now the pure-spec pre-validation runs before any db.add()
    at all, so a failed import creates nothing to roll back in the first
    place."""
    from opencashflow.models import CashflowSheet

    spec = _spec(sections=[{
        "name": "Ingresos", "rows": [
            {"name": "Ahorro", "rule": {"type": "percent_of_row", "row_id": "NoExiste", "percent": 10}},
        ],
    }])
    with pytest.raises(ValueError, match="NoExiste"):
        import_sheet_spec(db, spec, TEST_USER_ID)

    assert db.query(CashflowSheet).count() == 0


# ---------------------------------------------------------------------------
# carry_forward nesting rejected at the Pydantic layer (defense in depth --
# the engine's own runtime rejection is already covered by
# tests/test_engine_rules.py::test_carry_forward_nested_is_rejected_as_unsupported)
# ---------------------------------------------------------------------------

def test_nested_carry_forward_rejected_by_pydantic():
    with pytest.raises(pydantic.ValidationError):
        CarryForwardRuleSpec.model_validate({
            "type": "carry_forward",
            "base_rule": {"type": "carry_forward", "base_rule": {"type": "constant", "value": 1}},
        })


# ---------------------------------------------------------------------------
# Round-trip: export(import(spec)) reconstructs an equivalent SheetSpec,
# and re-importing the exported spec against seed_sheet's own output
# recomputes to the same values.
# ---------------------------------------------------------------------------

def test_round_trip_against_seed_sheet(db):
    """The strongest structural check available: seed_sheet() exercises
    every rule type in a real, tangled sum_rows hierarchy (Total Ingresos,
    Total Gastos Fijos, FLUJO NETO, SALDO FINAL all chain into each other).
    sheet_spec only captures row/rule STRUCTURE, not per-cell CellOverrides
    -- seed_sheet applies several (SALDO INICIAL's starting balance,
    seasonal bonuses), so comparing final COMPUTED values after reimport
    would fail for a reason unrelated to sheet_spec itself. Compare
    structure instead, via a double round-trip (export -> import -> export
    again) -- if that's stable, every rule/reference in the real example
    survived intact."""
    original = seed_sheet(db, user_id=TEST_USER_ID, months=6, base_period=date(2026, 1, 1))
    exported_once = export_sheet_spec(db, original)

    db2 = sessionmaker(bind=db.get_bind())()
    reimported = import_sheet_spec(db2, exported_once, user_id=TEST_USER_ID)
    exported_twice = export_sheet_spec(db2, reimported)

    assert exported_twice.sheet == exported_once.sheet
    assert exported_twice.sections == exported_once.sections
    db2.close()


def test_round_trip_preserves_row_and_section_names(db):
    spec = _spec(sections=[{
        "name": "Ingresos", "section_type": "income", "rows": [
            {"name": "Sueldo", "row_type": "input", "sign": "positive", "rule": {"type": "constant", "value": 5}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)
    exported = export_sheet_spec(db, sheet)
    assert exported.sections[0].name == "Ingresos"
    assert exported.sections[0].section_type == "income"
    assert exported.sections[0].rows[0].name == "Sueldo"
    assert exported.sections[0].rows[0].rule == ConstantRuleSpec(value=5)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def test_yaml_round_trip(tmp_path):
    spec = _spec(sections=[{"name": "Ingresos", "rows": [{"name": "Sueldo", "rule": {"type": "constant", "value": 5}}]}])
    path = str(tmp_path / "spec.yaml")
    dump_sheet_spec(spec, path)
    loaded = load_sheet_spec(path)
    assert loaded == spec


def test_yml_extension_also_works(tmp_path):
    spec = _spec()
    path = str(tmp_path / "spec.yml")
    dump_sheet_spec(spec, path)
    assert load_sheet_spec(path) == spec


def test_json_round_trip(tmp_path):
    spec = _spec(sections=[{"name": "Ingresos", "rows": [{"name": "Sueldo", "rule": {"type": "constant", "value": 5}}]}])
    path = str(tmp_path / "spec.json")
    dump_sheet_spec(spec, path)
    loaded = load_sheet_spec(path)
    assert loaded == spec


def test_unrecognized_extension_raises_on_load_and_dump(tmp_path):
    spec = _spec()
    path = str(tmp_path / "spec.txt")
    with pytest.raises(ValueError, match="Extensión no reconocida"):
        dump_sheet_spec(spec, path)
    (tmp_path / "spec.txt").write_text("{}")
    with pytest.raises(ValueError, match="Extensión no reconocida"):
        load_sheet_spec(path)


def test_serialize_sheet_spec_unrecognized_format_raises():
    with pytest.raises(ValueError, match="Formato no reconocido"):
        serialize_sheet_spec(_spec(), "xml")


def test_base_period_accepts_full_iso_date():
    spec = SheetMetaSpec(name="X", base_period="2026-03-15")
    assert spec.base_period == date(2026, 3, 1)


def test_base_period_accepts_year_month():
    spec = SheetMetaSpec(name="X", base_period="2026-03")
    assert spec.base_period == date(2026, 3, 1)


def test_base_period_rejects_garbage():
    with pytest.raises(pydantic.ValidationError):
        SheetMetaSpec(name="X", base_period="not-a-date")


# ---------------------------------------------------------------------------
# row_type/sign/section_type validation -- this file's Pydantic layer used to
# be the WEAKEST of the three entry points into these columns (the CLI's
# argparse choices= and the interactive wizard already validated them; a
# hand-edited YAML/JSON sheet spec did not). A bad value here used to pass
# straight through to the database and only surface, silently wrong, deep
# inside engine.py (e.g. row_sign_multiplier/row.sign resolution).
# ---------------------------------------------------------------------------

def test_bad_row_type_rejected_at_import(db):
    with pytest.raises(pydantic.ValidationError):
        _spec(sections=[{"name": "S", "rows": [{"name": "A", "row_type": "subtotall"}]}])


def test_bad_sign_rejected_at_import(db):
    with pytest.raises(pydantic.ValidationError):
        _spec(sections=[{"name": "S", "rows": [{"name": "A", "sign": "Positive"}]}])


def test_bad_section_type_rejected_at_import(db):
    with pytest.raises(pydantic.ValidationError):
        _spec(sections=[{"name": "S", "section_type": "custome", "rows": []}])


# ---------------------------------------------------------------------------
# seed_value / find_unseeded_running_balance_rows -- a running_balance row
# whose rule reads previous_period has nothing to read at period 1, so
# without an explicit seed the whole projection used to be silently built
# on an assumed starting balance of $0, with no error anywhere (confirmed
# end-to-end against docs/examples/hogar-chileno.yaml, which now sets
# seed_value on SALDO INICIAL to close exactly this gap).
# ---------------------------------------------------------------------------

def test_seed_value_writes_an_override_on_the_first_period(db):
    spec = _spec(sections=[{
        "name": "Saldo", "rows": [
            {"name": "SALDO INICIAL", "row_type": "running_balance", "seed_value": 850000,
             "rule": {"type": "previous_period", "row_id": "SALDO FINAL"}},
            {"name": "SALDO FINAL", "row_type": "running_balance",
             "rule": {"type": "sum_rows", "row_ids": ["SALDO INICIAL"]}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)

    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][0]["cells"]
    assert [c.projected_value for c in cells] == [850000, 850000, 850000]
    assert find_unseeded_running_balance_rows(db, sheet.id) == []


def test_unseeded_running_balance_row_is_detected(db):
    spec = _spec(sections=[{
        "name": "Saldo", "rows": [
            {"name": "SALDO INICIAL", "row_type": "running_balance",
             "rule": {"type": "previous_period", "row_id": "SALDO FINAL"}},
            {"name": "SALDO FINAL", "row_type": "running_balance",
             "rule": {"type": "sum_rows", "row_ids": ["SALDO INICIAL"]}},
        ],
    }])
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)

    # Reproduces the original bug end-to-end: no seed means period 1 is None.
    result = compute_sheet(sheet.id, db)
    cells = result["sections"][0]["rows"][0]["cells"]
    assert cells[0].projected_value is None

    unseeded = find_unseeded_running_balance_rows(db, sheet.id)
    assert [r.name for r in unseeded] == ["SALDO INICIAL"]


def test_hogar_chileno_example_has_a_real_starting_balance(db):
    """End-to-end against the maintainer's own documented example file --
    the exact scenario the original bug report was traced through."""
    example_path = Path(__file__).resolve().parent.parent / "docs" / "examples" / "hogar-chileno.yaml"
    spec = load_sheet_spec(str(example_path))
    sheet = import_sheet_spec(db, spec, TEST_USER_ID)

    result = compute_sheet(sheet.id, db)
    saldo_inicial = next(
        row_data for section in result["sections"] for row_data in section["rows"]
        if row_data["row"].name == "SALDO INICIAL"
    )
    assert saldo_inicial["cells"][0].projected_value == 850000
    assert find_unseeded_running_balance_rows(db, sheet.id) == []
