"""A declarative file format (YAML or JSON) describing a whole cashflow
sheet -- sheet metadata, sections, rows, and each row's projection rule --
in one document, instead of many CLI calls or the interactive wizard.

The rule shapes here are the SAME ones opencashflow.engine.compute_sheet
already reads (constant/previous_period/sum_rows/percent_of_row/
rolling_average/carry_forward), with the same key names (value, row_id,
row_ids, percent, n, base_rule) -- the only difference is that row_id/
row_ids hold ROW NAMES (str) instead of ints, since a row's real id
doesn't exist yet when the file is written. import_sheet_spec resolves
every name to a real id in a second pass, after every row has been
created and flushed.

Two-pass construction, same shape as opencashflow.seed.seed_sheet: pass 1
creates the CashflowSheet, calls generate_periods, and creates every
SheetSection/SheetRow with default_projection_rule=None, then flushes so
ids exist. seed.py never needed a name->row map (its rows are Python
variables, known at write time); a spec file only has names, so building
that map here is the genuinely new part. Pass 2 walks the spec again,
resolves every name reference (including one nested inside a
carry_forward's own base_rule) against that map, and only then writes
default_projection_rule. A duplicate row name anywhere in the sheet, or
an unresolvable name reference, raises ValueError naming the row that
caused it -- never a silent guess.

export_sheet_spec is the reverse direction (id -> name), so an existing
sheet's structure can be versioned in git, diffed, or used as the source
for an example spec file (see docs/examples/).
"""
import json
from datetime import date, datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from opencashflow.models import CashflowSheet, SheetRow, SheetSection
from opencashflow.periods import generate_periods

# ---------------------------------------------------------------------------
# Rule specs -- one Pydantic model per opencashflow.engine rule type, field
# names matching the engine's own rule dict keys exactly (see engine.py's
# _evaluate_rule docstring), except row_id/row_ids hold names, not ids.
# ---------------------------------------------------------------------------


class ConstantRuleSpec(BaseModel):
    type: Literal["constant"] = "constant"
    value: Union[int, float]


class PreviousPeriodRuleSpec(BaseModel):
    type: Literal["previous_period"] = "previous_period"
    row_id: Optional[str] = None  # a row NAME; None = this same row (engine's own default)


class SumRowsRuleSpec(BaseModel):
    type: Literal["sum_rows"] = "sum_rows"
    row_ids: List[str] = Field(default_factory=list)  # row NAMEs; empty is valid (no operands yet)


class PercentOfRowRuleSpec(BaseModel):
    type: Literal["percent_of_row"] = "percent_of_row"
    row_id: str  # a row NAME, required
    percent: Union[int, float]  # whole-number percent, e.g. 15 means 15% -- not a 0-1 fraction


class RollingAverageRuleSpec(BaseModel):
    type: Literal["rolling_average"] = "rolling_average"
    n: int = Field(gt=0)  # always this row's own history -- no row_id key, matching the engine


# The other 5 types, WITHOUT carry_forward -- used as CarryForwardRuleSpec's
# own base_rule type, so nesting carry_forward inside carry_forward is a
# Pydantic validation error, before opencashflow.engine's own runtime check
# (which rejects it with error="unsupported_rule:carry_forward(nested)")
# ever sees it.
NonCarryForwardRuleSpec = Annotated[
    Union[
        ConstantRuleSpec,
        PreviousPeriodRuleSpec,
        SumRowsRuleSpec,
        PercentOfRowRuleSpec,
        RollingAverageRuleSpec,
    ],
    Field(discriminator="type"),
]


class CarryForwardRuleSpec(BaseModel):
    type: Literal["carry_forward"] = "carry_forward"
    # Deliberately wider than cli.py's _CARRY_FORWARD_BASE_TYPES (which only
    # offers constant/sum_rows as --rule-base-type choices for CLI flag
    # ergonomics) -- the engine itself accepts any of the other 5 types as a
    # carry_forward base, and this spec format matches the engine's real
    # capability, not the flag's narrower subset.
    base_rule: NonCarryForwardRuleSpec


RuleSpec = Annotated[
    Union[
        ConstantRuleSpec,
        PreviousPeriodRuleSpec,
        SumRowsRuleSpec,
        PercentOfRowRuleSpec,
        RollingAverageRuleSpec,
        CarryForwardRuleSpec,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Sheet spec: sheet -> sections -> rows
# ---------------------------------------------------------------------------


class RowSpec(BaseModel):
    name: str
    row_type: str = "input"
    sign: str = "positive"
    rule: Optional[RuleSpec] = None


class SectionSpec(BaseModel):
    name: str
    section_type: str = "custom"
    rows: List[RowSpec] = Field(default_factory=list)


class SheetMetaSpec(BaseModel):
    name: str
    currency: str = "USD"
    horizon_months: int = 12
    base_period: date

    @field_validator("base_period", mode="before")
    @classmethod
    def _parse_base_period(cls, value: Any) -> Any:
        """Accepts "YYYY-MM" (matching the rest of this CLI's --base-period
        flag) or a full ISO "YYYY-MM-DD"/date/datetime -- always floored to
        day=1, matching CashflowSheet.base_period's own convention."""
        if isinstance(value, datetime):
            return value.date().replace(day=1)
        if isinstance(value, date):
            return value.replace(day=1)
        if isinstance(value, str):
            parts = value.split("-")
            if len(parts) in (2, 3):
                try:
                    year, month = int(parts[0]), int(parts[1])
                    return date(year, month, 1)
                except ValueError:
                    pass
        raise ValueError(f"base_period debe ser 'YYYY-MM' o 'YYYY-MM-DD', se recibió: {value!r}")


class SheetSpec(BaseModel):
    sheet: SheetMetaSpec
    sections: List[SectionSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Import (two-pass) / export
# ---------------------------------------------------------------------------


def _resolve_name(name: str, name_to_row: Dict[str, SheetRow], referencing_row_name: str) -> int:
    row = name_to_row.get(name)
    if row is None:
        raise ValueError(
            f"La fila '{referencing_row_name}' hace referencia a la fila '{name}', que no existe "
            f"en este sheet spec -- ninguna fila con ese nombre fue definida."
        )
    return row.id


def _resolve_rule_spec(
    rule: Union[ConstantRuleSpec, PreviousPeriodRuleSpec, SumRowsRuleSpec, PercentOfRowRuleSpec,
                RollingAverageRuleSpec, CarryForwardRuleSpec],
    name_to_row: Dict[str, SheetRow],
    referencing_row_name: str,
) -> dict:
    if isinstance(rule, ConstantRuleSpec):
        return {"type": "constant", "value": rule.value}
    if isinstance(rule, PreviousPeriodRuleSpec):
        result: Dict[str, Any] = {"type": "previous_period"}
        if rule.row_id is not None:
            result["row_id"] = _resolve_name(rule.row_id, name_to_row, referencing_row_name)
        return result
    if isinstance(rule, SumRowsRuleSpec):
        return {
            "type": "sum_rows",
            "row_ids": [_resolve_name(name, name_to_row, referencing_row_name) for name in rule.row_ids],
        }
    if isinstance(rule, PercentOfRowRuleSpec):
        return {
            "type": "percent_of_row",
            "row_id": _resolve_name(rule.row_id, name_to_row, referencing_row_name),
            "percent": rule.percent,
        }
    if isinstance(rule, RollingAverageRuleSpec):
        return {"type": "rolling_average", "n": rule.n}
    if isinstance(rule, CarryForwardRuleSpec):
        return {
            "type": "carry_forward",
            "base_rule": _resolve_rule_spec(rule.base_rule, name_to_row, referencing_row_name),
        }
    raise AssertionError(f"Tipo de regla no reconocido (no debería pasar validación de Pydantic): {rule!r}")


def import_sheet_spec(db: Session, spec: SheetSpec, user_id: int) -> CashflowSheet:
    """Build a whole CashflowSheet -- sections, rows, and every row's
    projection rule -- from a validated SheetSpec, in two passes (see this
    module's own docstring). Raises ValueError (never sys.exit, this is
    library-shaped) on a duplicate row name anywhere in the sheet, or an
    unresolvable row_id/row_ids name reference."""
    base_period_dt = datetime(spec.sheet.base_period.year, spec.sheet.base_period.month, 1)
    sheet = CashflowSheet(
        user_id=user_id, name=spec.sheet.name, currency=spec.sheet.currency,
        horizon_months=spec.sheet.horizon_months, base_period=base_period_dt,
    )
    db.add(sheet)
    db.flush()
    generate_periods(sheet, db)
    db.flush()

    name_to_row: Dict[str, SheetRow] = {}
    for section_idx, section_spec in enumerate(spec.sections):
        section = SheetSection(
            sheet_id=sheet.id, name=section_spec.name, section_type=section_spec.section_type,
            sort_order=section_idx,
        )
        db.add(section)
        db.flush()
        for row_idx, row_spec in enumerate(section_spec.rows):
            if row_spec.name in name_to_row:
                raise ValueError(
                    f"Nombre de fila duplicado: '{row_spec.name}' aparece más de una vez en este "
                    f"sheet spec -- cada fila necesita un nombre único para que las referencias "
                    f"(row_id/row_ids) se puedan resolver sin ambigüedad."
                )
            row = SheetRow(
                section_id=section.id, name=row_spec.name, row_type=row_spec.row_type,
                sign=row_spec.sign, sort_order=row_idx, default_projection_rule=None,
            )
            db.add(row)
            name_to_row[row_spec.name] = row
    db.flush()

    for section_spec in spec.sections:
        for row_spec in section_spec.rows:
            if row_spec.rule is not None:
                row = name_to_row[row_spec.name]
                row.default_projection_rule = _resolve_rule_spec(row_spec.rule, name_to_row, row_spec.name)
    db.commit()
    db.refresh(sheet)
    return sheet


def _reverse_resolve_name(row_id: int, id_to_name: Dict[int, str], referencing_row_name: str) -> str:
    name = id_to_name.get(row_id)
    if name is None:
        raise ValueError(
            f"La fila '{referencing_row_name}' referencia row_id={row_id}, que no existe en esta "
            f"planilla -- estado imposible del motor, no se puede exportar."
        )
    return name


def _reverse_resolve_rule(rule: dict, id_to_name: Dict[int, str], referencing_row_name: str):
    rule_type = rule.get("type")
    if rule_type == "constant":
        return ConstantRuleSpec(value=rule["value"])
    if rule_type == "previous_period":
        row_id = rule.get("row_id")
        return PreviousPeriodRuleSpec(
            row_id=_reverse_resolve_name(row_id, id_to_name, referencing_row_name) if row_id is not None else None,
        )
    if rule_type == "sum_rows":
        return SumRowsRuleSpec(
            row_ids=[_reverse_resolve_name(rid, id_to_name, referencing_row_name) for rid in rule.get("row_ids", [])],
        )
    if rule_type == "percent_of_row":
        return PercentOfRowRuleSpec(
            row_id=_reverse_resolve_name(rule["row_id"], id_to_name, referencing_row_name),
            percent=rule["percent"],
        )
    if rule_type == "rolling_average":
        return RollingAverageRuleSpec(n=rule["n"])
    if rule_type == "carry_forward":
        return CarryForwardRuleSpec(
            base_rule=_reverse_resolve_rule(rule["base_rule"], id_to_name, referencing_row_name),
        )
    raise ValueError(f"La fila '{referencing_row_name}' tiene un tipo de regla no reconocido: '{rule_type}'.")


def export_sheet_spec(db: Session, sheet: CashflowSheet) -> SheetSpec:
    """Reverse direction of import_sheet_spec: an existing sheet's sections/
    rows/rules become a SheetSpec (id -> name). Useful for versioning a
    sheet's structure in git, diffing structural changes, or generating an
    example spec from an existing sheet instead of writing one by hand."""
    sections = (
        db.query(SheetSection).filter(SheetSection.sheet_id == sheet.id).order_by(SheetSection.sort_order).all()
    )
    rows_by_section = {
        section.id: (
            db.query(SheetRow).filter(SheetRow.section_id == section.id).order_by(SheetRow.sort_order).all()
        )
        for section in sections
    }
    id_to_name = {
        row.id: row.name for rows in rows_by_section.values() for row in rows
    }

    section_specs = []
    for section in sections:
        row_specs = []
        for row in rows_by_section[section.id]:
            rule_spec = (
                _reverse_resolve_rule(row.default_projection_rule, id_to_name, row.name)
                if row.default_projection_rule else None
            )
            row_specs.append(RowSpec(name=row.name, row_type=row.row_type, sign=row.sign, rule=rule_spec))
        section_specs.append(SectionSpec(
            name=section.name, section_type=section.section_type, rows=row_specs,
        ))

    return SheetSpec(
        sheet=SheetMetaSpec(
            name=sheet.name, currency=sheet.currency, horizon_months=sheet.horizon_months,
            base_period=sheet.base_period.date(),
        ),
        sections=section_specs,
    )


# ---------------------------------------------------------------------------
# File I/O -- format dispatched by extension, .yaml/.yml or .json
# ---------------------------------------------------------------------------


def _format_from_extension(path: str) -> str:
    if path.endswith((".yaml", ".yml")):
        return "yaml"
    if path.endswith(".json"):
        return "json"
    raise ValueError(f"Extensión no reconocida para un sheet spec: '{path}' -- usa .yaml, .yml o .json.")


def serialize_sheet_spec(spec: SheetSpec, fmt: str) -> str:
    """Serialize to a string in the given format ("yaml" or "json") without
    touching the filesystem -- used by `sheet export` when printing to
    stdout instead of writing a file."""
    data = spec.model_dump(mode="json", exclude_none=True)
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)
    raise ValueError(f"Formato no reconocido: '{fmt}' -- usa 'yaml' o 'json'.")


def load_sheet_spec(path: str) -> SheetSpec:
    fmt = _format_from_extension(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) if fmt == "yaml" else json.load(f)
    return SheetSpec.model_validate(raw)


def dump_sheet_spec(spec: SheetSpec, path: str) -> None:
    fmt = _format_from_extension(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(serialize_sheet_spec(spec, fmt))
