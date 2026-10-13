"""Add sections and rows to a sheet, edit a row, and check a projection rule
before it's stored -- the primitives behind the CLI's `section add`/
`row add`/`row edit` (and the wizard's equivalents), reusable as-is by any
other consumer (an HTTP endpoint) that needs the exact same guarantees:
append at the end of the section/sheet by default instead of silently
reordering it, and never store a rule the engine can't evaluate safely.

Extracted out of cli.py, where they lived as `_do_add_section`/
`_do_add_row`/`_do_edit_row` (cli.py keeps those names as aliases) -- same
move as opencashflow.overrides / opencashflow.record_ops.

validate_rule is new with the extraction. The CLI builds every rule by
resolving rows within the sheet, so it can't produce a bad one; a caller
sending a rule as data (an HTTP body) can, and the engine is lenient in two
ways that make that dangerous: a reference to a row outside the sheet just
reads as nothing (a silently understated sum -- `doctor` only reports it
after the fact), and a non-dict carry_forward base_rule raises
AttributeError out of compute_sheet, taking the whole sheet down. An
unknown rule TYPE is deliberately still allowed: the engine reports it per
cell as unsupported_rule:<type>, visibly, by design.
"""
from typing import Any, Dict, List, Optional, Tuple, Union

from opencashflow.enums import ROW_SIGNS, ROW_TYPES
from opencashflow.models import CashflowSheet, SheetRow, SheetSection
from opencashflow.unset import UNSET, Unset

# UNSET (re-exported from opencashflow.unset): "this field was not provided
# -- leave it untouched". An explicit None for rule/ledger_mapping/notes
# means "clear it".
__all__ = ["UNSET", "rule_row_refs", "validate_rule", "add_section", "add_row", "edit_row"]


def rule_row_refs(rule: Any) -> List[Any]:
    """Every row id `rule` references, as stored: sum_rows' row_ids,
    percent_of_row's/previous_period's row_id, and a carry_forward's
    base_rule, recursively. Lenient on purpose (never raises, values not
    type-checked) so it can also inspect rules already in the database --
    validate_rule is the strict check for a rule about to be written."""
    if not isinstance(rule, dict):
        return []
    rule_type = rule.get("type")
    if rule_type == "sum_rows":
        row_ids = rule.get("row_ids", [])
        return list(row_ids) if isinstance(row_ids, list) else [row_ids]
    if rule_type in ("percent_of_row", "previous_period"):
        row_id = rule.get("row_id")
        return [row_id] if row_id is not None else []
    if rule_type == "carry_forward":
        return rule_row_refs(rule.get("base_rule"))
    return []


def _check_rule_shape(rule: Any) -> None:
    if not isinstance(rule, dict):
        raise ValueError(f"Una regla debe ser un objeto JSON, se recibió {rule!r}.")
    if rule.get("type") == "carry_forward":
        base_rule = rule.get("base_rule")
        if base_rule is not None:
            _check_rule_shape(base_rule)


def validate_rule(db, sheet_id: int, rule: Optional[Dict[str, Any]]) -> None:
    """Raise ValueError unless `rule` (None = no rule, always fine) is a
    dict, any carry_forward base_rule is too, and every row id it
    references (rule_row_refs) is an integer naming a row of sheet
    `sheet_id`."""
    if rule is None:
        return
    _check_rule_shape(rule)
    refs = rule_row_refs(rule)
    not_ints = [r for r in refs if not isinstance(r, int) or isinstance(r, bool)]
    if not_ints:
        raise ValueError(f"La regla referencia filas con ids que no son enteros: {not_ints!r}.")
    if not refs:
        return
    sheet_row_ids = {
        row_id for (row_id,) in
        db.query(SheetRow.id).join(SheetSection).filter(SheetSection.sheet_id == sheet_id)
    }
    missing = sorted(set(refs) - sheet_row_ids)
    if missing:
        raise ValueError(f"La regla referencia fila(s) que no existen en la planilla #{sheet_id}: {missing}.")


def add_section(
    db, sheet: CashflowSheet, name: str, section_type: str = "custom", sort_order: Optional[int] = None, *,
    is_collapsible: bool = True, color_hex: Optional[str] = None,
) -> SheetSection:
    """Add a section to `sheet`. Commits. Default sort_order is one past the
    current max in the sheet (append at the end) -- NOT 0, which would
    silently reorder every existing section visually. Raises ValueError for
    an unknown section_type (the model's own validator)."""
    if sort_order is None:
        existing = db.query(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
        sort_order = 0 if not existing else max(s.sort_order for s in existing) + 1
    section = SheetSection(
        sheet_id=sheet.id, name=name, section_type=section_type, sort_order=sort_order,
        is_collapsible=is_collapsible, color_hex=color_hex,
    )
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def add_row(
    db, section: SheetSection, name: str, row_type: str = "input", sign: str = "positive",
    rule: Optional[Dict[str, Any]] = None, sort_order: Optional[int] = None, *,
    is_visible: bool = True, ledger_mapping: Optional[Dict[str, Any]] = None, notes: Optional[str] = None,
) -> SheetRow:
    """Add a row to `section`. Commits. Default sort_order is one past the
    current max WITHIN that section (append at the end), same reasoning as
    add_section. Raises ValueError -- before writing anything -- for a rule
    validate_rule rejects, or an unknown row_type/sign (the model's own
    validators)."""
    validate_rule(db, section.sheet_id, rule)
    if sort_order is None:
        existing = db.query(SheetRow).filter(SheetRow.section_id == section.id).all()
        sort_order = 0 if not existing else max(r.sort_order for r in existing) + 1
    row = SheetRow(
        section_id=section.id, name=name, row_type=row_type, sign=sign,
        default_projection_rule=rule, sort_order=sort_order,
        is_visible=is_visible, ledger_mapping=ledger_mapping, notes=notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def edit_row(
    db, row: SheetRow, *,
    name: Union[str, Unset] = UNSET,
    row_type: Union[str, Unset] = UNSET,
    sign: Union[str, Unset] = UNSET,
    rule: Union[Optional[Dict[str, Any]], Unset] = UNSET,
    sort_order: Union[int, Unset] = UNSET,
    is_visible: Union[bool, Unset] = UNSET,
    ledger_mapping: Union[Optional[Dict[str, Any]], Unset] = UNSET,
    notes: Union[Optional[str], Unset] = UNSET,
) -> Dict[str, Tuple[Any, Any]]:
    """Partial update of `row`: only fields actually passed (not left at
    UNSET) are considered, and only ones that actually change something are
    applied. `rule=None` explicitly clears default_projection_rule (distinct
    from not passing `rule` at all); same for ledger_mapping/notes.

    Everything is validated before anything is touched -- an invalid
    row_type/sign or a rule validate_rule rejects raises ValueError with
    `row` unchanged. Commits only if something actually changed. Returns
    {field_name: (old_value, new_value)} for every field that changed --
    the "rule" key is for default_projection_rule -- so the caller can
    print an exact "antes -> después" summary without re-deriving it.
    """
    if not isinstance(row_type, Unset) and row_type not in ROW_TYPES:
        raise ValueError(f"row_type debe ser uno de {ROW_TYPES}, se recibió {row_type!r}")
    if not isinstance(sign, Unset) and sign not in ROW_SIGNS:
        raise ValueError(f"sign debe ser uno de {ROW_SIGNS}, se recibió {sign!r}")
    if not isinstance(rule, Unset):
        validate_rule(db, row.section.sheet_id, rule)

    requested: Dict[str, Tuple[str, Any]] = {
        "name": ("name", name),
        "row_type": ("row_type", row_type),
        "sign": ("sign", sign),
        "rule": ("default_projection_rule", rule),
        "sort_order": ("sort_order", sort_order),
        "is_visible": ("is_visible", is_visible),
        "ledger_mapping": ("ledger_mapping", ledger_mapping),
        "notes": ("notes", notes),
    }
    changes: Dict[str, Tuple[Any, Any]] = {}
    for field, (attr, new) in requested.items():
        if isinstance(new, Unset):
            continue
        old = getattr(row, attr)
        if new != old:
            changes[field] = (old, new)
            setattr(row, attr, new)
    if changes:
        db.commit()
        db.refresh(row)
    return changes
