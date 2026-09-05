"""Tests for opencashflow.sheet_spec: the Pydantic rule/sheet models, the
two-pass import_sheet_spec/export_sheet_spec loader, and file I/O
(load_sheet_spec/dump_sheet_spec/serialize_sheet_spec).
"""
from datetime import date

import pydantic
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import compute_sheet
from opencashflow.models import Base
from opencashflow.seed import seed_sheet
from opencashflow.sheet_spec import (
    CarryForwardRuleSpec,
    ConstantRuleSpec,
    SheetMetaSpec,
    SheetSpec,
    dump_sheet_spec,
    export_sheet_spec,
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
