"""Generic sheet-engine CLI command handlers: sheet/section/row CRUD, the
projection table renderer, record/override read-write, export, available,
period close, wallets, and the interactive wizard.

Migrated out of the private opencashflow-app's backend/cli.py (2026-09-05):
every function here was verified to depend on nothing but opencashflow.* --
no Chilean bank/credit-card/bicicleta content, no private ledger/auth model.
These are library-shaped command handlers (they take `args`/`db` and print
to stdout, never `sys.exit` except where noted) -- a consuming app's own
CLI wires up argparse and calls into these; this module does not build its
own ArgumentParser (see PENDIENTES.md for that as a separate, deliberately
sequenced next step -- splitting the parser/dispatch skeleton is a bigger,
riskier change than moving already-working functions verbatim).

Any reference in these functions' own help/error text to "python -m
backend.cli ..." is not stale: today these functions are still only reachable
through a consuming app's own CLI entry point (e.g. opencashflow-cli's `ocf`),
since this module has no standalone entry point of its own yet.
"""
import csv
import dataclasses
import os
import re
import shutil
import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from opencashflow.cli_export import export_csv, export_xlsx
from opencashflow.engine import (
    build_sum_rows_hierarchy as _build_sum_rows_hierarchy,
    compute_sheet,
    effective_sign_to_top as _effective_sign_to_top,
    row_sign_multiplier as _row_sign_multiplier,
)
from opencashflow.models import (
    CashflowSheet,
    CellActualEntry,
    CellOverride,
    SheetCell,
    SheetPeriod,
    SheetRow,
    SheetSection,
)
from opencashflow.period_close import AGGREGATE_ROW_TYPES as _AGGREGATE_ROW_TYPES
from opencashflow.period_close import CloseReport, close_period
from opencashflow.period_close import find_balance_row as _find_balance_row_or_raise
from opencashflow.periods import extend_periods_backward, find_anchor_period, generate_periods
from opencashflow.record_stack import (
    guard_periods_not_closed as _guard_periods_not_closed,
    pop_record_stack as _pop_record_stack,
    replay_record_stack as _replay_record_stack,
)
from opencashflow.wallet import Wallet, WalletMovement
from opencashflow.wallet_movements import do_wallet_movement_add, do_wallet_movement_undo


def _parse_base_period(value: str):
    if not value:
        return None
    year, month = value.split("-")
    return date(int(year), int(month), 1)




def _pick_sheet(db, sheet_id: int) -> CashflowSheet:
    if sheet_id is not None:
        sheet = db.query(CashflowSheet).filter(CashflowSheet.id == sheet_id).first()
        if not sheet:
            print(f"No existe la planilla #{sheet_id}.", file=sys.stderr)
            sys.exit(1)
        return sheet
    sheet = db.query(CashflowSheet).order_by(CashflowSheet.created_at.desc(), CashflowSheet.id.desc()).first()
    if not sheet:
        print("No hay planillas todavía. Corre primero: python -m backend.cli seed", file=sys.stderr)
        sys.exit(1)
    return sheet




def _print_row_candidates(candidates, row_arg: str) -> None:
    print(f"'{row_arg}' es ambiguo, coincide con {len(candidates)} filas:", file=sys.stderr)
    for r in candidates:
        print(f"  [{r.id}] {r.section.name} > {r.name}", file=sys.stderr)
    print("Usa --row-id o un nombre más específico.", file=sys.stderr)




def _match_by_name(items, needle: str, name_of=lambda item: item.name) -> list:
    """Two-stage name match shared by every name-resolving lookup in this
    CLI: exact case-insensitive match first; if that finds nothing, fall
    back to substring. Returns whichever stage produced results (possibly
    more than one, possibly zero) -- the caller decides what "more than one"
    means for it (hard error for _resolve_row/_resolve_section, a numbered
    pick-list for the wizard's interactive resolvers). Keeping the matching
    semantics in one place means both call sites can never drift apart on
    what counts as "the same fila" -- only what happens on ambiguity does.
    """
    needle_cf = needle.casefold()
    exact = [item for item in items if name_of(item).casefold() == needle_cf]
    if exact:
        return exact
    return [item for item in items if needle_cf in name_of(item).casefold()]




def _resolve_row(db, sheet_id: int, row_arg: str) -> SheetRow:
    """Resolve a --row argument (numeric id, exact name, or substring) to a
    SheetRow within the given sheet. Never guesses on ambiguity — a
    financial tool that silently picked "the best match" between two
    similarly-named rows would be worse than one that just asks again.
    """
    if row_arg.isdigit():
        row = (
            db.query(SheetRow)
            .join(SheetSection)
            .filter(SheetRow.id == int(row_arg), SheetSection.sheet_id == sheet_id)
            .first()
        )
        if not row:
            print(f"No existe la fila #{row_arg} en la planilla #{sheet_id}.", file=sys.stderr)
            sys.exit(1)
        return row

    rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet_id).all()
    matches = _match_by_name(rows, row_arg)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _print_row_candidates(matches, row_arg)
        sys.exit(1)

    print(f"No se encontró ninguna fila que coincida con '{row_arg}' en la planilla #{sheet_id}.", file=sys.stderr)
    print(f"Corre 'python -m backend.cli show --sheet-id {sheet_id} --show-ids' para ver las filas disponibles.",
          file=sys.stderr)
    sys.exit(1)




def _print_section_candidates(candidates, section_arg: str) -> None:
    print(f"'{section_arg}' es ambiguo, coincide con {len(candidates)} secciones:", file=sys.stderr)
    for s in candidates:
        print(f"  [{s.id}] {s.name}", file=sys.stderr)
    print("Usa el id de la sección o un nombre más específico.", file=sys.stderr)




def _resolve_section(db, sheet_id: int, section_arg: str) -> SheetSection:
    """Resolve a --section argument (numeric id, exact name, or substring) to
    a SheetSection within the given sheet. Same never-guess philosophy as
    _resolve_row: exact case-insensitive match first, then substring, and a
    hard error (candidates to stderr, sys.exit(1)) on more than one match at
    either stage.
    """
    if section_arg.isdigit():
        section = (
            db.query(SheetSection)
            .filter(SheetSection.id == int(section_arg), SheetSection.sheet_id == sheet_id)
            .first()
        )
        if not section:
            print(f"No existe la sección #{section_arg} en la planilla #{sheet_id}.", file=sys.stderr)
            sys.exit(1)
        return section

    sections = db.query(SheetSection).filter(SheetSection.sheet_id == sheet_id).all()
    matches = _match_by_name(sections, section_arg)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _print_section_candidates(matches, section_arg)
        sys.exit(1)

    print(f"No se encontró ninguna sección que coincida con '{section_arg}' en la planilla #{sheet_id}.",
          file=sys.stderr)
    print(f"Corre 'python -m backend.cli sections --sheet-id {sheet_id}' para ver las secciones disponibles.",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Wizard-only interactive resolvers
# ---------------------------------------------------------------------------
#
# The ONE place in this CLI allowed to handle name/section ambiguity by
# showing a numbered pick-list instead of a hard error -- because there's an
# interactive human right there to answer. _resolve_row/_resolve_section
# (above) and every _do_xxx internal MUST keep failing hard on ambiguity, since
# a non-interactive caller has no way to answer a menu; these wrappers exist
# only for backend.cli's wizard commands. Same two-stage matching (via
# _match_by_name) as the hard-error versions -- only what happens next
# differs. Return None (after printing why) instead of exiting the process,
# so a wizard menu action can just abort itself and return to the menu.



def _wizard_resolve_row(db, sheet_id: int, row_arg: str) -> Optional[SheetRow]:
    if row_arg.isdigit():
        row = (
            db.query(SheetRow)
            .join(SheetSection)
            .filter(SheetRow.id == int(row_arg), SheetSection.sheet_id == sheet_id)
            .first()
        )
        if not row:
            print(f"  No existe la fila #{row_arg} en esta planilla.")
        return row

    rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet_id).all()
    matches = _match_by_name(rows, row_arg)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"  '{row_arg}' es ambiguo, coincide con {len(matches)} filas:")
        for i, r in enumerate(matches, 1):
            print(f"    {i}) [{r.id}] {r.section.name} > {r.name}")
        choice = input("  Elige un número (o enter para cancelar): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        print("  Cancelado.")
        return None

    print(f"  No se encontró ninguna fila que coincida con '{row_arg}'.")
    return None




def _wizard_resolve_section(db, sheet_id: int, section_arg: str) -> Optional[SheetSection]:
    if section_arg.isdigit():
        section = (
            db.query(SheetSection)
            .filter(SheetSection.id == int(section_arg), SheetSection.sheet_id == sheet_id)
            .first()
        )
        if not section:
            print(f"  No existe la sección #{section_arg} en esta planilla.")
        return section

    sections = db.query(SheetSection).filter(SheetSection.sheet_id == sheet_id).all()
    matches = _match_by_name(sections, section_arg)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"  '{section_arg}' es ambiguo, coincide con {len(matches)} secciones:")
        for i, s in enumerate(matches, 1):
            print(f"    {i}) [{s.id}] {s.name}")
        choice = input("  Elige un número (o enter para cancelar): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        print("  Cancelado.")
        return None

    print(f"  No se encontró ninguna sección que coincida con '{section_arg}'.")
    return None




def _describe_rule(rule: Optional[dict], row_id_to_name: Dict[int, str]) -> str:
    """Human-readable Spanish description of a projection rule.

    `row_id_to_name` should map every row_id in the sheet to its name (build
    it once per command invocation from all of the sheet's rows -- never
    requery per row). A row_id referenced by a rule but missing from the map
    is shown as "row_id <N> (no encontrada)" instead of crashing.
    """
    if not rule or rule.get("type") in (None, "empty"):
        return "(sin regla)"

    rule_type = rule.get("type")

    def row_name(row_id) -> str:
        return row_id_to_name.get(row_id, f"row_id {row_id} (no encontrada)")

    if rule_type == "constant":
        value = rule.get("value")
        value_text = _fmt_number(Decimal(str(value)), "1") if value is not None else "(sin valor)"
        return f"constante: {value_text}"

    if rule_type == "previous_period":
        row_id = rule.get("row_id")
        name = "esta misma" if row_id is None else row_name(row_id)
        return f"igual al período anterior de '{name}'"

    if rule_type == "sum_rows":
        names = [row_name(rid) for rid in rule.get("row_ids", [])]
        return "suma de: " + (", ".join(names) if names else "(sin filas)")

    if rule_type == "percent_of_row":
        row_id = rule.get("row_id")
        percent = rule.get("percent")
        name = "(fila no especificada)" if row_id is None else row_name(row_id)
        percent_text = "?" if percent is None else str(percent)
        return f"{percent_text}% de '{name}'"

    if rule_type == "rolling_average":
        n = rule.get("n")
        n_text = "?" if n is None else str(n)
        return f"promedio móvil de los últimos {n_text} períodos"

    if rule_type == "carry_forward":
        base_desc = _describe_rule(rule.get("base_rule"), row_id_to_name)
        return f"{base_desc} + arrastre de lo pendiente del período anterior"

    return f"regla no reconocida: {rule_type}"


# ---------------------------------------------------------------------------
# Rule building (typed --rule-* flags -> JSON rule dicts)
# ---------------------------------------------------------------------------
#
# Shared by `row add`, `row edit` and `override set` -- NEVER expose raw JSON
# rule construction on the command line, every rule is built from these typed
# flags. `_add_rule_args` wires the flags onto an argparse subparser;
# `_build_rule_from_args` turns a parsed Namespace into the exact JSON shape
# opencashflow.engine expects (see engine.py's module docstring for the
# canonical field names -- e.g. sum_rows uses "row_ids", not "rows").

_RULE_TYPES = ["constant", "previous_period", "sum_rows", "percent_of_row", "rolling_average", "carry_forward"]
_CARRY_FORWARD_BASE_TYPES = ["constant", "sum_rows"]

# Module-level so both build_parser() (argparse choices=) and the wizard
# (text prompts/validation) share the exact same list -- never duplicated.
_ROW_TYPE_CHOICES = [
    "input", "data", "formula", "subtotal", "total", "running_balance", "label", "separator",
]

# _CARD_STATUS_CHOICES (credit-card status validation) is Chile/credit-card
# specific -- it stays in the private app's own cli.py, not here.




def _add_rule_args(parser) -> None:
    """Add the shared --rule-* flags to a subparser (`row add`, `row edit`,
    `override set`). Every flag defaults to None; giving none of them at all
    means "no rule" (or, for `row edit`, "don't touch the rule")."""
    parser.add_argument("--rule-type", choices=_RULE_TYPES, default=None,
                         help="Tipo de regla de proyección (default: sin regla / sin cambio)")
    parser.add_argument("--rule-value", type=str, default=None, help="constant: valor")
    parser.add_argument("--rule-row", type=str, default=None,
                         help="previous_period/percent_of_row: nombre (o parte) o id de la fila referenciada "
                              "(previous_period: si se omite, se refiere a esta misma fila)")
    parser.add_argument("--rule-rows", type=str, default=None,
                         help="sum_rows: nombres o ids de filas separados por coma, ej. \"Sueldo,Freelance,3\"")
    parser.add_argument("--rule-percent", type=str, default=None, help="percent_of_row: porcentaje")
    parser.add_argument("--rule-n", type=int, default=None, help="rolling_average: cantidad de períodos")
    parser.add_argument("--rule-base-type", choices=_CARRY_FORWARD_BASE_TYPES, default=None,
                         help="carry_forward: tipo de la regla base (soportados por ahora: constant, sum_rows -- "
                              "percent_of_row/rolling_average como base no están implementados)")
    parser.add_argument("--rule-base-value", type=str, default=None, help="carry_forward con base constant: valor")
    parser.add_argument("--rule-base-rows", type=str, default=None,
                         help="carry_forward con base sum_rows: nombres o ids de filas separados por coma")




def _parse_rule_number(value: str, flag_name: str):
    """Parse a --rule-* numeric flag into a plain int or float -- never a
    Decimal, since default_projection_rule is a JSON column and json.dumps
    can't serialize Decimal. Returns int when the value is integral (matches
    the plain numbers used everywhere else in this codebase's rule dicts,
    see opencashflow.seed), float otherwise. Exits with a clear message on a
    malformed value instead of letting a cryptic ValueError escape."""
    try:
        d = Decimal(value)
    except Exception:
        print(f"{flag_name}: '{value}' no es un número válido.", file=sys.stderr)
        sys.exit(1)
    return int(d) if d == d.to_integral_value() else float(d)




def _resolve_row_list(db, sheet_id: int, csv_value: str) -> List[int]:
    """Resolve a comma-separated --rule-rows/--rule-base-rows value into a
    list of row ids, via _resolve_row (one at a time, against the row's own
    sheet) -- so an ambiguous or unknown entry names exactly which one is
    the problem, never silently dropped or guessed."""
    names = [n.strip() for n in csv_value.split(",") if n.strip()]
    if not names:
        print("La lista de filas está vacía.", file=sys.stderr)
        sys.exit(1)
    return [_resolve_row(db, sheet_id, name).id for name in names]




def _constant_rule(value_str: str, flag_name: str) -> dict:
    return {"type": "constant", "value": _parse_rule_number(value_str, flag_name)}




def _sum_rows_rule(db, sheet_id: int, csv_value: str) -> dict:
    return {"type": "sum_rows", "row_ids": _resolve_row_list(db, sheet_id, csv_value)}




def _build_carry_forward_base_rule(db, sheet_id: int, args) -> dict:
    # Validation/error messages stay specific to --rule-base-*, but the
    # actual dict shape is built by the SAME _constant_rule/_sum_rows_rule
    # helpers _build_rule_from_args uses below -- one place defines what a
    # "constant"/"sum_rows" rule dict looks like, not two.
    base_type = args.rule_base_type
    if not base_type:
        print("--rule-type carry_forward requiere --rule-base-type.", file=sys.stderr)
        sys.exit(1)
    if base_type == "constant":
        if args.rule_base_value is None:
            print("--rule-base-type constant requiere --rule-base-value.", file=sys.stderr)
            sys.exit(1)
        return _constant_rule(args.rule_base_value, "--rule-base-value")
    if base_type == "sum_rows":
        if not args.rule_base_rows:
            print("--rule-base-type sum_rows requiere --rule-base-rows.", file=sys.stderr)
            sys.exit(1)
        return _sum_rows_rule(db, sheet_id, args.rule_base_rows)
    # Unreachable while argparse's choices=_CARRY_FORWARD_BASE_TYPES stays in
    # sync with this function, but guarded anyway rather than crashing
    # opaquely if that ever drifts -- percent_of_row/rolling_average as a
    # carry_forward base are a known, documented limitation, not supported.
    print(
        f"--rule-base-type '{base_type}' no está soportado como base de carry_forward "
        f"(soportados: {', '.join(_CARRY_FORWARD_BASE_TYPES)}).",
        file=sys.stderr,
    )
    sys.exit(1)




def _build_rule_from_args(db, sheet_id: int, args) -> Optional[dict]:
    """Build a projection-rule dict from the shared --rule-* flags (see
    _add_rule_args), resolving any row reference via _resolve_row against
    the given sheet. Returns None when --rule-type wasn't given at all --
    that's "no rule" for `row add`/`override set`, and "don't touch the
    rule" for `row edit` (the caller there checks args.rule_type itself
    before calling this, since None is also a valid explicit rule value).
    """
    rule_type = args.rule_type
    if not rule_type:
        return None

    if rule_type == "constant":
        if args.rule_value is None:
            print("--rule-type constant requiere --rule-value.", file=sys.stderr)
            sys.exit(1)
        return _constant_rule(args.rule_value, "--rule-value")

    if rule_type == "previous_period":
        rule: Dict = {"type": "previous_period"}
        if args.rule_row:
            rule["row_id"] = _resolve_row(db, sheet_id, args.rule_row).id
        return rule

    if rule_type == "sum_rows":
        if not args.rule_rows:
            print("--rule-type sum_rows requiere --rule-rows \"a,b,c\".", file=sys.stderr)
            sys.exit(1)
        return _sum_rows_rule(db, sheet_id, args.rule_rows)

    if rule_type == "percent_of_row":
        if not args.rule_row or args.rule_percent is None:
            print("--rule-type percent_of_row requiere --rule-row y --rule-percent.", file=sys.stderr)
            sys.exit(1)
        row_id = _resolve_row(db, sheet_id, args.rule_row).id
        return {"type": "percent_of_row", "row_id": row_id, "percent": _parse_rule_number(args.rule_percent, "--rule-percent")}

    if rule_type == "rolling_average":
        if args.rule_n is None:
            print("--rule-type rolling_average requiere --rule-n.", file=sys.stderr)
            sys.exit(1)
        return {"type": "rolling_average", "n": args.rule_n}

    if rule_type == "carry_forward":
        base_rule = _build_carry_forward_base_rule(db, sheet_id, args)
        return {"type": "carry_forward", "base_rule": base_rule}

    # Unreachable while argparse's choices=_RULE_TYPES stays in sync with
    # this function -- guarded anyway per this codebase's "never crash
    # opaquely" convention.
    print(f"Tipo de regla no reconocido: {rule_type}", file=sys.stderr)
    sys.exit(1)


# Tipos de fila que son agregados (sumas de otras filas), no obligaciones
# propias -- se excluyen de "cuánto tengo pendiente este período". Definición
# canónica ahora en backend/period_close.py (importada arriba como
# _AGGREGATE_ROW_TYPES) para que la CLI y el endpoint HTTP nunca diverjan.




def _find_balance_row(db, sheet_id: int) -> SheetRow:
    """Detecta la fila 'saldo inicial' de la planilla por patrón (la única con
    regla previous_period), sin asumir un nombre fijo -- mismo principio que
    _resolve_row: nunca adivina si hay ambigüedad.

    Delegado a opencashflow.period_close.find_balance_row (misma consulta,
    única fuente de verdad para la CLI y el endpoint HTTP) -- aquí sólo se
    traduce el ValueError a la convención de esta CLI (stderr +
    sys.exit(1)), ya que es código de librería que un handler HTTP también
    llama y no debe terminar el proceso.
    """
    try:
        return _find_balance_row_or_raise(db, sheet_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


# _row_sign_multiplier/_build_sum_rows_hierarchy/_effective_sign_to_top now
# live in backend/bridge_financing.py (imported above) -- draw_for_deficit
# needs them too, for the same "how does this row really affect the bottom
# line" question `available`/`show --with-real` already answer with them.

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Deliberately conservative: only the ONE color everyone agrees on the
# meaning of (negative = deficit/shortfall = red) plus bold for structural
# headers -- not a full palette, since "expense magnitude" being big is not
# the same claim as "bad" (a large Gastos Variables total isn't wrong), so
# tinting every positive number green would be actively misleading.
_ANSI = {"red": "\033[31m", "orange": "\033[38;5;208m", "bold": "\033[1m", "dim": "\033[2m"}
_ANSI_RESET = "\033[0m"




def _colors_enabled() -> bool:
    # NO_COLOR (https://no-color.org) always wins if set to anything. Absent
    # that, only color when stdout is an actual terminal -- piping into a
    # file/`less`/another program must see plain text, never raw escape
    # codes mixed into the numbers.
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()




def _c(text: str, *styles: str) -> str:
    """Wrap already-final text in ANSI color codes -- callers MUST apply
    this AFTER any column-width padding (f"{text:>{width}}"), never before:
    padding counts the invisible escape bytes as visible characters and
    would misalign every column the instant color is layered in first."""
    if not text or not _colors_enabled():
        return text
    codes = "".join(_ANSI[s] for s in styles)
    return f"{codes}{text}{_ANSI_RESET}"




def _fmt_number(value, unit: str) -> str:
    if value is None:
        return "—"
    divisor = Decimal(1000) if unit == "k" else Decimal(1)
    scaled = (value / divisor).to_integral_value(rounding=ROUND_HALF_UP)
    n = int(scaled)
    sign = "-" if n < 0 else ""
    text = f"{abs(n):,d}".replace(",", ".")
    return f"{sign}{text}"




def _period_label(period) -> str:
    return period.label or period.period_date.strftime("%b-%y")




def _render_table(
    sheet, result, periods, unit: str, width, show_ids: bool, anchor_period_id=None,
    real_values: Optional[Dict[int, Optional[Decimal]]] = None, real_label: Optional[str] = None,
    balance_breakdown: Optional[List[Tuple[str, Decimal, bool]]] = None,
    balance_row_id: Optional[int] = None, combined_total: Optional[Decimal] = None,
) -> None:
    term_width = width or shutil.get_terminal_size(fallback=(100, 24)).columns

    def header_label(p) -> str:
        base = _period_label(p)
        return f"▸{base}" if anchor_period_id is not None and p.id == anchor_period_id else base

    # La columna "real" solo se agrega junto al período ancla (--with-real),
    # nunca una por período -- son los valores actualizados de HOY, no una
    # proyección real por cada mes futuro.
    show_real = real_values is not None and any(p.id == anchor_period_id for p in periods)

    resolved_rows = []
    for section_data in result["sections"]:
        section = section_data["section"]
        for row_data in section_data["rows"]:
            row = row_data["row"]
            cells_by_period = {c.period_id: c for c in row_data["cells"]}
            cell_texts = []
            for p in periods:
                cell = cells_by_period.get(p.id)
                if cell is None:
                    cell_texts.append(("—", False))
                elif cell.error == "cycle_detected":
                    cell_texts.append(("!ciclo", False))
                elif cell.error and cell.error.startswith("unsupported_rule"):
                    cell_texts.append(("!regla", False))
                else:
                    cell_texts.append((_fmt_number(cell.projected_value, unit), cell.effective_source == "manual"))
            real_text = _fmt_number(real_values.get(row.id), unit) if show_real else None
            # Solo en la fila de saldo, y solo cuando hay algo que sumarle:
            # "223.253 (387.721)" -- caja sola, y entre paréntesis el total
            # si además contamos el cupo de tarjetas disponible ahora
            # (billeteras + crédito, ver _wallets_total_for_currency/
            # _credit_total_for_currency en cmd_show). Deliberadamente
            # aparte de lo que real_values ya trae: ESE número sigue
            # alimentando SALDO FINAL/FLUJO NETO más abajo en la tabla
            # (por eso solo suma el cupo YA retirado por el bridge, ver
            # _compute_real_column_values) -- este paréntesis es puramente
            # informativo, "esto es lo que también podrías usar", y nunca
            # se mezcla con esa cuenta.
            if real_text is not None and row.id == balance_row_id and combined_total is not None:
                real_text = f"{real_text} ({_fmt_number(combined_total, unit)})"
            resolved_rows.append((section, row, cell_texts, real_text))

    max_len = max([len(header_label(p)) for p in periods], default=8)
    if show_real and real_label:
        max_len = max(max_len, len(real_label))
    for _section, _row, cell_texts, real_text in resolved_rows:
        for text, marked in cell_texts:
            max_len = max(max_len, len(text) + (1 if marked else 0))
        if real_text is not None:
            max_len = max(max_len, len(real_text))
    col_width = max(8, max_len)

    # Ancho de la columna de nombres: dinámico, no un número fijo -- un
    # nombre de fila (con su prefijo "(−) "/"    ", 4 caracteres siempre)
    # que superara un ancho fijo se salía de su columna y corría todas las
    # cifras de esa fila hacia la derecha respecto a las demás (confirmado
    # contra una fila real: "Financiamiento puente: retiro de cupo" con
    # prefijo mide 42 caracteres). Mínimo 28 para no angostar tablas con
    # nombres cortos.
    label_width = max(
        28,
        max((len(f"    {r.name}") for _s, r, _c, _rt in resolved_rows), default=0),
        max((len(f"    [{r.id}] {r.name}") for _s, r, _c, _rt in resolved_rows), default=0) if show_ids else 0,
    )

    unit_label = "cifras en M$" if unit == "k" else "cifras en pesos"
    print(f"{sheet.name} — planilla #{sheet.id} — {sheet.currency} — {unit_label}\n")

    extra_cols = 1 if show_real else 0
    total_width = label_width + (len(periods) + extra_cols) * (col_width + 1)
    if total_width > term_width:
        print(
            f"[aviso] la tabla necesita ~{total_width} columnas y el terminal tiene {term_width}. "
            f"Prueba --unit k, --months menor, o --format csv.\n"
        )

    # Encabezado en 2 líneas cuando hay columna "Actual": la línea 1 solo
    # trae los períodos de siempre (sin repetir el mes en la columna
    # nueva -- ya está a la izquierda, en la columna del período ancla);
    # la línea 2 va en blanco salvo bajo esa columna extra, donde dice
    # "Actual" sola (nunca entre paréntesis).
    header_line1 = " " * label_width
    header_line2 = " " * label_width
    for p in periods:
        header_line1 += f"{header_label(p):>{col_width + 1}}"
        header_line2 += " " * (col_width + 1)  # en blanco bajo la columna propia del período
        if show_real and p.id == anchor_period_id:
            header_line1 += " " * (col_width + 1)  # en blanco bajo la columna extra, línea 1
            header_line2 += f"{real_label:>{col_width + 1}}"  # "Actual" bajo la columna extra, línea 2
    print(header_line1)
    if show_real:
        print(header_line2)

    used_manual = False
    used_error = False
    current_section = None
    for section, row, cell_texts, real_text in resolved_rows:
        if section is not current_section:
            current_section = section
            if section.section_type != "balance":
                print(f"  {_c(f'── {section.name.upper()} ──', 'bold')}")
        is_expense = section.section_type == "expense"
        prefix = "(−) " if row.sign == "negative" else "    "
        name_display = f"[{row.id}] {row.name}" if show_ids else row.name
        label = f"{prefix}{name_display}"
        line = f"{label:<{label_width}}"
        for p, (text, marked) in zip(periods, cell_texts):
            if marked:
                used_manual = True
            if text in ("!ciclo", "!regla"):
                used_error = True
            display = f"{text}{'*' if marked else ''}"
            # El color se aplica DESPUÉS de rellenar el ancho de columna --
            # nunca antes, o los bytes invisibles del código ANSI se cuentan
            # como caracteres visibles y desalinean toda la tabla. Negativo
            # (rojo) siempre gana sobre "es un gasto" (naranjo) -- en la
            # práctica no se solapan, las filas de gasto nunca son las que
            # muestran déficit, esas viven en la sección Saldo.
            padded = f"{display:>{col_width + 1}}"
            if text.startswith("-"):
                line += _c(padded, "red")
            elif is_expense:
                line += _c(padded, "orange")
            else:
                line += padded
            if show_real and p.id == anchor_period_id:
                padded_real = f"{real_text:>{col_width + 1}}"
                if real_text.startswith("-"):
                    line += _c(padded_real, "red")
                elif is_expense:
                    line += _c(padded_real, "orange")
                else:
                    line += padded_real
        print(line)

    legend = []
    if anchor_period_id is not None and any(p.id == anchor_period_id for p in periods):
        legend.append("▸ = período actual")
    if used_manual:
        legend.append("* = override manual")
    legend.append("— = sin valor")
    if used_error:
        legend.append("! = error de cálculo")
    if show_real:
        legend.append(
            f"{real_label} = actualizado con lo real: SALDO INICIAL pasa a ser caja + cupo real de las "
            f"tarjetas del bridge de este período (ver detalle abajo) -- sin reservar lo facturado y aún sin "
            f"pagar como sí hace `available`; cada otra fila muestra lo que falta por resolver (facturado y aún "
            f"sin pagar, o el monto proyectado completo si todavía no hay ningún record)"
        )
    print("\n  " + "    ".join(legend))

    if show_real and balance_breakdown:
        parts = []
        total = Decimal(0)
        for label, value, is_estimate in balance_breakdown:
            tag = " (estimado)" if is_estimate else ""
            parts.append(f"{label}{tag}: {_fmt_number(value, unit)}")
            total += value
        print(f"\n  Detalle de SALDO INICIAL en la columna {real_label} = {' + '.join(parts)} = {_fmt_number(total, unit)}")




def _render_csv(sheet, result, periods, unit: str) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["Fila"] + [_period_label(p) for p in periods])
    for section in result["sections"]:
        for row_data in section["rows"]:
            row = row_data["row"]
            cells_by_period = {c.period_id: c for c in row_data["cells"]}
            values = []
            for p in periods:
                cell = cells_by_period.get(p.id)
                if cell is None or cell.projected_value is None:
                    values.append("")
                else:
                    divisor = Decimal(1000) if unit == "k" else Decimal(1)
                    values.append(str((cell.projected_value / divisor).to_integral_value(rounding=ROUND_HALF_UP)))
            writer.writerow([row.name] + values)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------



def _diagnose(result) -> list:
    issues = []
    cyclic_rows = set()
    unsupported = set()
    all_row_ids = set()

    for section in result["sections"]:
        for row_data in section["rows"]:
            row = row_data["row"]
            all_row_ids.add(row.id)
            for cell in row_data["cells"]:
                if cell.error == "cycle_detected":
                    cyclic_rows.add(row.name)
                elif cell.error and cell.error.startswith("unsupported_rule:"):
                    unsupported.add((row.name, cell.error.split(":", 1)[1]))

    for name in sorted(cyclic_rows):
        issues.append(f"Ciclo detectado en la fila '{name}': todas sus celdas quedan vacías.")
    for name, rule_type in sorted(unsupported):
        issues.append(
            f"La fila '{name}' usa la regla no soportada '{rule_type}' "
            f"(ver docs/cashflow-model.md) y por eso aparece vacía."
        )

    for section in result["sections"]:
        for row_data in section["rows"]:
            row = row_data["row"]
            rule = row.default_projection_rule
            if not rule:
                continue
            rule_type = rule.get("type")
            if rule_type == "sum_rows":
                refs = rule.get("row_ids", [])
            elif rule_type in ("percent_of_row", "previous_period"):
                rid = rule.get("row_id")
                refs = [rid] if rid is not None else []
            else:
                refs = []
            missing = [r for r in refs if r not in all_row_ids]
            if missing:
                issues.append(
                    f"La fila '{row.name}' referencia row_id(s) inexistente(s) {missing} — "
                    f"esa suma queda incompleta."
                )

    return issues


# ---------------------------------------------------------------------------
# Cell override write primitives (shared by `override set` / `override
# clear`, and reusable as-is by the later wizard phase)
# ---------------------------------------------------------------------------

@dataclasses.dataclass


class OverrideWriteResult:
    period: SheetPeriod
    override: Optional[CellOverride]  # None only when a --lock write was skipped
    previous_override: Optional[CellOverride] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


@dataclasses.dataclass


class OverrideClearResult:
    period: SheetPeriod
    previous_override: Optional[CellOverride]  # None means there was nothing active to clear
    reverted_value: Optional[Decimal] = None
    reverted_source: Optional[str] = None




def _print_override_write_results(
    row: SheetRow, results: List["OverrideWriteResult"], row_id_to_name: Dict[int, str], indent: str = "",
) -> None:
    """Shared by cmd_override_set and the wizard's 'escribir override' menu
    action -- one canonical rendering of an override-set result list, so the
    two surfaces can never print a different summary for the same write."""
    for result in results:
        label = _period_label(result.period)
        if result.skipped:
            print(f"{indent}[aviso] {row.name} [{label}]: {result.skip_reason} -- no se escribió ningún override.")
            continue
        ov = result.override
        line = f"{indent}[OK] {row.name} [{label}]: override {ov.override_type} = {_describe_override_value(ov, row_id_to_name)}"
        if result.previous_override is not None:
            prev_text = _describe_override_value(result.previous_override, row_id_to_name)
            line += f"  (reemplazó un override existente de {prev_text})"
        print(line)




def _print_override_clear_results(
    row: SheetRow, results: List["OverrideClearResult"], row_id_to_name: Dict[int, str], indent: str = "",
) -> None:
    """Shared by cmd_override_clear and the wizard's 'borrar override' menu
    action -- see _print_override_write_results."""
    for r in results:
        label = _period_label(r.period)
        if r.previous_override is None:
            print(f"{indent}{row.name} [{label}]: no había ningún override activo (nada que limpiar).")
            continue
        prev_text = _describe_override_value(r.previous_override, row_id_to_name)
        value_text = _fmt_number(r.reverted_value, "1") if r.reverted_value is not None else "sin valor"
        print(f"{indent}[OK] {row.name} [{label}]: se limpió el override ({prev_text})  ->  vuelve a: {value_text}")




def _describe_override_value(ov: CellOverride, row_id_to_name: Dict[int, str]) -> str:
    """Human-readable value of an override, for confirmation summaries --
    a manual_rule override has no `value` (it's in `custom_rule`), so this
    dispatches on override_type rather than just checking for None."""
    if ov.override_type == "manual_rule":
        return _describe_rule(ov.custom_rule, row_id_to_name)
    return _fmt_number(ov.value, "1") if ov.value is not None else "(sin valor)"




def _resolve_target_periods(
    db, sheet_id: int, period: Optional[str], from_period: Optional[str], to_period: Optional[str],
) -> List[SheetPeriod]:
    """Resolve --period XOR (--from-period AND --to-period) into the sheet's
    existing SheetPeriod rows for every calendar month in that (inclusive)
    range. Never auto-creates a period and never silently skips a missing
    month -- a range that includes a month the sheet doesn't have a period
    for is a hard error naming every missing month (run `backfill` first for
    historical gaps)."""
    if period and (from_period or to_period):
        print("Usa --period o --from-period/--to-period, no ambos.", file=sys.stderr)
        sys.exit(1)
    if not period and not (from_period and to_period):
        if from_period or to_period:
            print("--from-period y --to-period deben darse juntos.", file=sys.stderr)
        else:
            print("Debes dar --period, o --from-period y --to-period.", file=sys.stderr)
        sys.exit(1)

    target_dates: List[date] = []
    if period:
        target_dates.append(_parse_base_period(period))
    else:
        start = _parse_base_period(from_period)
        end = _parse_base_period(to_period)
        if end < start:
            print(f"--to-period ({to_period}) es anterior a --from-period ({from_period}).", file=sys.stderr)
            sys.exit(1)
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            target_dates.append(date(y, m, 1))
            m += 1
            if m == 13:
                m = 1
                y += 1

    periods: List[SheetPeriod] = []
    missing: List[str] = []
    for d in target_dates:
        dt = datetime(d.year, d.month, 1)
        p = db.query(SheetPeriod).filter(SheetPeriod.sheet_id == sheet_id, SheetPeriod.period_date == dt).first()
        if p is None:
            missing.append(d.strftime("%Y-%m"))
        else:
            periods.append(p)

    if missing:
        print(
            f"La planilla #{sheet_id} no tiene período(s) para: {', '.join(missing)}. "
            f"No se escribió/limpió ningún override (usa 'backfill' primero si son meses históricos).",
            file=sys.stderr,
        )
        sys.exit(1)

    return periods




def _supersede_and_write_override(
    db, cell: SheetCell, override_type: str, value: Optional[Decimal], custom_rule: Optional[dict],
    note: Optional[str], created_by: int,
) -> Tuple[CellOverride, Optional[CellOverride]]:
    """Supersede the cell's active override (if any) and insert a new one.
    Returns (new_override, previous_override) -- previous_override is
    returned (not just its value) so the caller can describe it correctly
    even for a manual_rule override, whose value column is always None."""
    previous = None
    for ov in cell.overrides:
        if ov.superseded_at is None:
            previous = ov
            break
    if previous is not None:
        previous.superseded_at = datetime.utcnow()
        db.flush()

    override = CellOverride(
        cell_id=cell.id, value=value, override_type=override_type,
        custom_rule=custom_rule, note=note, created_by=created_by,
    )
    db.add(override)
    db.flush()
    return override, previous




def _get_or_create_cell(db, row_id: int, period_id: int) -> SheetCell:
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    return cell




def _do_set_override(
    db, sheet_id: int, row: SheetRow, periods: List[SheetPeriod], *,
    value: Optional[Decimal] = None, rule: Optional[dict] = None, lock: bool = False,
    note: Optional[str] = None, created_by: int,
) -> List[OverrideWriteResult]:
    """Write the SAME override onto every period in `periods` for `row`.
    Exactly one of value/rule/lock is expected to be meaningful -- validated
    by the caller (cmd_override_set), since a later wizard caller already
    knows which one it wants and shouldn't have to fake the other two.

    --lock: computes the sheet ONCE up front (nothing this function does
    changes projected values before the lock writes, so one shared snapshot
    for the whole call is correct) and captures each targeted cell's current
    effective projected_value as the override's value -- NEVER None. A
    period whose captured value is None (nothing to lock yet) is skipped
    (skip_reason set, no override written for it), not silently written with
    value=None.

    Commits once at the end. Every write supersedes any existing active
    override on that cell first (see _supersede_and_write_override).
    """
    captured_by_period: Dict[int, Optional[Decimal]] = {}
    if lock:
        result = compute_sheet(sheet_id, db)
        for section_data in result["sections"]:
            for row_data in section_data["rows"]:
                if row_data["row"].id != row.id:
                    continue
                for cr in row_data["cells"]:
                    captured_by_period[cr.period_id] = cr.projected_value

    if lock:
        override_type = "lock"
    elif rule is not None:
        override_type = "manual_rule"
    else:
        override_type = "manual_value"

    results: List[OverrideWriteResult] = []
    for period in periods:
        if lock:
            captured_value = captured_by_period.get(period.id)
            if captured_value is None:
                results.append(OverrideWriteResult(
                    period=period, override=None, skipped=True,
                    skip_reason="la celda no tiene ningún valor efectivo todavía -- no hay nada que bloquear",
                ))
                continue
            ov_value, ov_rule = captured_value, None
        elif rule is not None:
            ov_value, ov_rule = None, rule
        else:
            ov_value, ov_rule = value, None

        cell = _get_or_create_cell(db, row.id, period.id)
        override, previous = _supersede_and_write_override(
            db, cell, override_type, ov_value, ov_rule, note, created_by,
        )
        results.append(OverrideWriteResult(period=period, override=override, previous_override=previous))

    db.commit()
    return results




def _do_clear_override(db, sheet_id: int, row: SheetRow, periods: List[SheetPeriod]) -> List[OverrideClearResult]:
    """Supersede the active override (if any) on every (row, period) cell in
    `periods`. A period with no active override is a no-op for it (reported,
    not an error). After clearing, recomputes the sheet ONCE and reports
    what each cell's value reverted to -- its own rule's result, or None
    ("sin valor") if it has none -- which is the whole point of the command.
    """
    results: List[OverrideClearResult] = []
    for period in periods:
        cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
        previous = None
        if cell is not None:
            for ov in cell.overrides:
                if ov.superseded_at is None:
                    previous = ov
                    break
            if previous is not None:
                previous.superseded_at = datetime.utcnow()
                db.flush()
        results.append(OverrideClearResult(period=period, previous_override=previous))
    db.commit()

    result = compute_sheet(sheet_id, db)
    cell_result_by_period: Dict[int, object] = {}
    for section_data in result["sections"]:
        for row_data in section_data["rows"]:
            if row_data["row"].id != row.id:
                continue
            for cr in row_data["cells"]:
                cell_result_by_period[cr.period_id] = cr

    for r in results:
        cr = cell_result_by_period.get(r.period.id)
        r.reverted_value = cr.projected_value if cr else None
        r.reverted_source = cr.effective_source if cr else None

    return results


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------



def cmd_sheets(db, args) -> None:
    sheets = db.query(CashflowSheet).order_by(CashflowSheet.created_at.desc()).all()
    if not sheets:
        print("No hay planillas. Corre: python -m backend.cli seed")
        return
    for sheet in sheets:
        print(
            f"[{sheet.id}] {sheet.name}  moneda={sheet.currency}  "
            f"horizonte={sheet.horizon_months}m  periodos={len(sheet.periods)}  usuario_id={sheet.user_id}"
        )




def cmd_doctor(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    result = compute_sheet(sheet.id, db)
    issues = _diagnose(result)
    if not issues:
        print(f"Planilla #{sheet.id} ('{sheet.name}'): sin problemas detectados.")
        return
    print(f"Planilla #{sheet.id} ('{sheet.name}'): {len(issues)} problema(s) detectado(s):")
    for issue in issues:
        print(f"  - {issue}")




def cmd_sections(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    sections = (
        db.query(SheetSection)
        .filter(SheetSection.sheet_id == sheet.id)
        .order_by(SheetSection.sort_order)
        .all()
    )
    if not sections:
        print(f"La planilla #{sheet.id} no tiene secciones.")
        return
    for section in sections:
        row_count = db.query(SheetRow).filter(SheetRow.section_id == section.id).count()
        print(
            f"[{section.id}] {section.name}  tipo={section.section_type}  "
            f"orden={section.sort_order}  filas={row_count}"
        )




def _do_add_section(
    db, sheet: CashflowSheet, name: str, section_type: str = "custom", sort_order: Optional[int] = None,
) -> SheetSection:
    """Add a section to `sheet`. Commits. Default sort_order is one past the
    current max in the sheet (append at the end) -- NOT 0, which would
    silently reorder every existing section visually."""
    if sort_order is None:
        existing = db.query(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
        sort_order = 0 if not existing else max(s.sort_order for s in existing) + 1
    section = SheetSection(sheet_id=sheet.id, name=name, section_type=section_type, sort_order=sort_order)
    db.add(section)
    db.commit()
    db.refresh(section)
    return section




def cmd_section_add(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    section = _do_add_section(db, sheet, args.name, args.type, args.sort_order)
    print(f"[OK] Sección creada: [{section.id}] {section.name}  tipo={section.section_type}  orden={section.sort_order}")
    print(
        f"Siguiente paso: python -m backend.cli row add --sheet-id {sheet.id} --section {section.id} --name \"...\""
    )




def cmd_rows(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)

    query = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id)
    section = None
    if args.section:
        section = _resolve_section(db, sheet.id, args.section)
        query = query.filter(SheetRow.section_id == section.id)
    rows = query.order_by(SheetSection.sort_order, SheetRow.sort_order).all()

    if not rows:
        if section:
            print(f"La sección '{section.name}' no tiene filas.")
        else:
            print(f"La planilla #{sheet.id} no tiene filas.")
        return

    # Nombres de TODAS las filas de la planilla (no solo las que se listan) --
    # una regla puede referenciar una fila de otra sección.
    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}

    for row in rows:
        rule_text = _describe_rule(row.default_projection_rule, row_id_to_name)
        print(
            f"[{row.id}] {row.section.name} > {row.name}  tipo={row.row_type}  "
            f"signo={row.sign}  regla: {rule_text}"
        )




def _do_add_row(
    db, section: SheetSection, name: str, row_type: str = "input", sign: str = "positive",
    rule: Optional[dict] = None, sort_order: Optional[int] = None,
) -> SheetRow:
    """Add a row to `section`. Commits. Default sort_order is one past the
    current max WITHIN that section (append at the end), same reasoning as
    _do_add_section."""
    if sort_order is None:
        existing = db.query(SheetRow).filter(SheetRow.section_id == section.id).all()
        sort_order = 0 if not existing else max(r.sort_order for r in existing) + 1
    row = SheetRow(
        section_id=section.id, name=name, row_type=row_type, sign=sign,
        default_projection_rule=rule, sort_order=sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row




def cmd_row_add(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    section = _resolve_section(db, sheet.id, args.section)
    rule = _build_rule_from_args(db, sheet.id, args)
    row = _do_add_row(db, section, args.name, args.row_type, args.sign, rule, args.sort_order)

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    rule_text = _describe_rule(row.default_projection_rule, row_id_to_name)

    print(
        f"[OK] Fila creada: [{row.id}] {section.name} > {row.name}  tipo={row.row_type}  "
        f"signo={row.sign}  regla: {rule_text}"
    )
    print("Nota: --sign solo importa si esta fila es referenciada dentro del sum_rows de otra fila.")


# Sentinel distinguishing "this field wasn't passed at all, don't touch it"
# from a real value (crucially, from an explicit `rule=None`, which means
# "clear the rule" -- see _do_edit_row and cmd_row_edit's --clear-rule).
_UNSET = object()




def _do_edit_row(
    db, row: SheetRow, *, name=_UNSET, row_type=_UNSET, sign=_UNSET, rule=_UNSET, sort_order=_UNSET,
) -> Dict[str, Tuple]:
    """Partial update of `row`: only fields actually passed (i.e. not left
    at the _UNSET default) are considered, and only ones that actually
    change something are applied -- mirrors the HTTP RowUpdate/update_row
    `model_dump(exclude_unset=True)` pattern, but operating directly on the
    ORM row rather than through that Pydantic schema. `rule=None` explicitly
    clears default_projection_rule (distinct from not passing `rule` at
    all). Commits only if something actually changed. Returns
    {field_name: (old_value, new_value)} for every field that changed --
    field_name is one of "name"/"row_type"/"sign"/"rule"/"sort_order" (the
    "rule" one is for default_projection_rule) -- so the caller can print an
    exact "antes -> después" summary without re-deriving what changed itself.
    """
    changes: Dict[str, Tuple] = {}
    if name is not _UNSET and name != row.name:
        changes["name"] = (row.name, name)
        row.name = name
    if row_type is not _UNSET and row_type != row.row_type:
        changes["row_type"] = (row.row_type, row_type)
        row.row_type = row_type
    if sign is not _UNSET and sign != row.sign:
        changes["sign"] = (row.sign, sign)
        row.sign = sign
    if rule is not _UNSET and rule != row.default_projection_rule:
        changes["rule"] = (row.default_projection_rule, rule)
        row.default_projection_rule = rule
    if sort_order is not _UNSET and sort_order != row.sort_order:
        changes["sort_order"] = (row.sort_order, sort_order)
        row.sort_order = sort_order
    if changes:
        db.commit()
        db.refresh(row)
    return changes




def cmd_row_edit(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)

    if args.clear_rule and args.rule_type:
        print("No puedes usar --clear-rule junto con --rule-type: elige uno de los dos.", file=sys.stderr)
        sys.exit(1)

    kwargs = {}
    if args.name is not None:
        kwargs["name"] = args.name
    if args.row_type is not None:
        kwargs["row_type"] = args.row_type
    if args.sign is not None:
        kwargs["sign"] = args.sign
    if args.sort_order is not None:
        kwargs["sort_order"] = args.sort_order
    if args.clear_rule:
        kwargs["rule"] = None
    elif args.rule_type:
        kwargs["rule"] = _build_rule_from_args(db, sheet.id, args)

    changes = _do_edit_row(db, row, **kwargs)

    if not changes:
        print(f"Fila [{row.id}] '{row.name}': no se pasó ningún cambio.")
        return

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}

    print(f"[OK] Fila [{row.id}] '{row.name}' actualizada:")
    for field, (old, new) in changes.items():
        if field == "rule":
            print(f"  regla: {_describe_rule(old, row_id_to_name)}  ->  {_describe_rule(new, row_id_to_name)}")
        else:
            print(f"  {field}: {old!r}  ->  {new!r}")




def cmd_backfill(db, args) -> None:
    if args.months <= 0:
        print("--months debe ser mayor que 0.", file=sys.stderr)
        sys.exit(1)

    sheet = _pick_sheet(db, args.sheet_id)
    try:
        created = extend_periods_backward(sheet, args.months, db)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    # extend_periods_backward() only flushes (per its own docstring) --
    # commit here, same as every other write command in this CLI.
    db.commit()

    if not created:
        print(
            f"Planilla #{sheet.id}: no se creó ningún período nuevo "
            f"(los {args.months} mes(es) calendario solicitados ya existían)."
        )
        return

    print(f"Planilla #{sheet.id}: se crearon {len(created)} período(s):")
    for period in sorted(created, key=lambda p: p.sort_order):
        print(f"  [{period.id}] {_period_label(period)}  ({period.period_date.strftime('%Y-%m')})")

    if len(created) < args.months:
        print(
            f"  [aviso] se pidieron {args.months} mes(es) pero sólo se crearon {len(created)}: "
            f"el/los mes(es) calendario restante(s) ya existía(n) en la planilla."
        )




def cmd_record_set(db, args) -> None:
    if args.actual is None and args.accrued is None and args.paid is None:
        print("Debes dar al menos uno de --actual, --accrued, --paid.", file=sys.stderr)
        sys.exit(1)

    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)

    period_date = _parse_base_period(args.period) if args.period else date.today().replace(day=1)
    period_dt = datetime(period_date.year, period_date.month, 1)
    period = db.query(SheetPeriod).filter(
        SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == period_dt,
    ).first()
    if not period:
        print(f"La planilla #{sheet.id} no tiene un período para {period_dt.strftime('%Y-%m')}.", file=sys.stderr)
        sys.exit(1)

    cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
    if not cell:
        cell = SheetCell(row_id=row.id, period_id=period.id)
        db.add(cell)
        db.flush()

    previous_actual = cell.actual_value
    if args.actual is not None:
        cell.actual_value = Decimal(str(args.actual))
    if args.accrued is not None:
        cell.accrued_value = Decimal(str(args.accrued))
    if args.paid is not None:
        cell.paid_value = Decimal(str(args.paid))

    user_id = args.user_id if args.user_id is not None else sheet.user_id
    db.add(CellActualEntry(
        cell_id=cell.id,
        actual_value=cell.actual_value,
        accrued_value=cell.accrued_value,
        paid_value=cell.paid_value,
        note=args.note,
        created_by=user_id,
    ))
    db.commit()

    result = compute_sheet(sheet.id, db)
    cell_result = None
    for section in result["sections"]:
        for row_data in section["rows"]:
            if row_data["row"].id == row.id:
                for cr in row_data["cells"]:
                    if cr.period_id == period.id:
                        cell_result = cr

    projected_text = _fmt_number(cell_result.projected_value if cell_result else None, args.unit)
    actual_text = _fmt_number(cell.actual_value, args.unit)
    variance_text = _fmt_number(cell_result.variance if cell_result else None, args.unit)
    before_text = _fmt_number(previous_actual, args.unit) if previous_actual is not None else "—"

    print(f"[OK] {row.name} [row_id={row.id}] — {_period_label(period)}: actual={actual_text} (antes: {before_text})")
    print(f"  Proyectado: {projected_text}   Variación: {variance_text}")


# ---------------------------------------------------------------------------
# record history / undo / clear -- the "real" layer's own history +
# undo/clear, symmetric to override's set/clear. `record set` (above) and
# every other writer of CellActualEntry (bridge_financing.draw_for_deficit,
# creditcard_statements.sync_period's real-statement branch,
# opencashflow.wallet_movements' own writes) all append to the SAME
# append-only log -- none of them are special-cased here, an undo walks
# back through whichever kind of entry it finds. The stack-replay mechanics
# themselves (replay_record_stack, guard_periods_not_closed,
# pop_record_stack) now live in opencashflow.record_stack (imported above) --
# a public, generic primitive, no longer app-specific plumbing.
# ---------------------------------------------------------------------------



@dataclasses.dataclass
class RecordHistoryEntry:
    entry: CellActualEntry
    is_current: bool




def _do_record_history(db, row: SheetRow, period: SheetPeriod) -> List[RecordHistoryEntry]:
    cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
    if cell is None:
        return []
    live = (cell.actual_value, cell.accrued_value, cell.paid_value)
    views = []
    current_index = None
    for i, entry in enumerate(cell.actual_entries):
        if (entry.actual_value, entry.accrued_value, entry.paid_value) == live:
            current_index = i  # el último que calce gana -- el historial puede repetir valores
        views.append(RecordHistoryEntry(entry=entry, is_current=False))
    if current_index is not None:
        views[current_index].is_current = True
    return views


@dataclasses.dataclass


class RecordUndoResult:
    period: SheetPeriod
    reverted_from: CellActualEntry
    reverted_to: Optional[CellActualEntry]  # None significa "vuelve a sin valor registrado"
    new_entry: CellActualEntry




def _do_record_undo(db, sheet_id: int, row: SheetRow, period: SheetPeriod, *, note: Optional[str], created_by: int) -> RecordUndoResult:
    _guard_periods_not_closed([period], "deshacer un record")

    cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
    if cell is None or not cell.actual_entries:
        raise ValueError(
            f"'{row.name}' en {_period_label(period)} no tiene ningún record registrado -- no hay nada que deshacer."
        )

    try:
        reverted_from, reverted_to, new_entry = _pop_record_stack(db, cell, note=note, created_by=created_by)
    except ValueError:
        raise ValueError(
            f"'{row.name}' en {_period_label(period)}: ya se deshicieron todos los cambios registrados -- "
            f"no queda nada más que deshacer."
        )

    db.commit()
    db.refresh(new_entry)

    return RecordUndoResult(period=period, reverted_from=reverted_from, reverted_to=reverted_to, new_entry=new_entry)


@dataclasses.dataclass


class RecordClearResult:
    period: SheetPeriod
    cleared: bool
    previous: Optional[Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]] = None




def _do_record_clear(
    db, sheet_id: int, row: SheetRow, periods: List[SheetPeriod], *, note: Optional[str], created_by: int,
) -> List[RecordClearResult]:
    _guard_periods_not_closed(periods, "limpiar un record")

    results: List[RecordClearResult] = []
    for period in periods:
        cell = db.query(SheetCell).filter(SheetCell.row_id == row.id, SheetCell.period_id == period.id).first()
        if cell is None or (cell.actual_value is None and cell.accrued_value is None and cell.paid_value is None):
            results.append(RecordClearResult(period=period, cleared=False))
            continue

        previous = (cell.actual_value, cell.accrued_value, cell.paid_value)
        cell.actual_value = None
        cell.accrued_value = None
        cell.paid_value = None
        write_note = note or "[record clear] limpieza manual del valor real -- vuelve a sin valor registrado"
        db.add(CellActualEntry(
            cell_id=cell.id, actual_value=None, accrued_value=None, paid_value=None,
            note=write_note, created_by=created_by,
        ))
        results.append(RecordClearResult(period=period, cleared=True, previous=previous))

    db.commit()
    return results




def cmd_record_undo(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)
    period_date = _parse_base_period(args.period) if args.period else date.today().replace(day=1)
    period_dt = datetime(period_date.year, period_date.month, 1)
    period = db.query(SheetPeriod).filter(SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == period_dt).first()
    if period is None:
        print(f"La planilla #{sheet.id} no tiene un período para {period_dt.strftime('%Y-%m')}.", file=sys.stderr)
        sys.exit(1)

    user_id = args.user_id if args.user_id is not None else sheet.user_id
    try:
        result = _do_record_undo(db, sheet.id, row, period, note=args.note, created_by=user_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    from_text = (
        f"actual={_fmt_number(result.reverted_from.actual_value, args.unit)} "
        f"accrued={_fmt_number(result.reverted_from.accrued_value, args.unit)} "
        f"paid={_fmt_number(result.reverted_from.paid_value, args.unit)}"
    )
    if result.reverted_to is None:
        to_text = "sin valor registrado"
    else:
        to_text = (
            f"actual={_fmt_number(result.reverted_to.actual_value, args.unit)} "
            f"accrued={_fmt_number(result.reverted_to.accrued_value, args.unit)} "
            f"paid={_fmt_number(result.reverted_to.paid_value, args.unit)}"
        )
    print(f"[OK] '{row.name}' — {_period_label(period)}: se deshizo el último cambio.")
    print(f"  Antes de deshacer: {from_text}")
    print(f"  Ahora:             {to_text}")




def cmd_record_clear(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)
    periods = _resolve_target_periods(db, sheet.id, args.period, args.from_period, args.to_period)
    user_id = args.user_id if args.user_id is not None else sheet.user_id

    try:
        results = _do_record_clear(db, sheet.id, row, periods, note=args.note, created_by=user_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    for r in results:
        label = _period_label(r.period)
        if not r.cleared:
            print(f"'{row.name}' [{label}]: no había ningún valor real registrado (nada que limpiar).")
            continue
        a, ac, p = r.previous
        print(
            f"[OK] '{row.name}' [{label}]: se limpió (actual={_fmt_number(a, args.unit)} "
            f"accrued={_fmt_number(ac, args.unit)} paid={_fmt_number(p, args.unit)})  ->  sin valor registrado"
        )




def cmd_override_set(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)

    # Exactly one of --value / rule flags / --lock -- never guess which one
    # was meant when the combination is ambiguous or empty.
    has_value = args.value is not None
    has_rule = args.rule_type is not None
    has_lock = args.lock
    chosen = sum([has_value, has_rule, has_lock])
    if chosen == 0:
        print("Debes dar exactamente uno de --value, --rule-type ..., o --lock.", file=sys.stderr)
        sys.exit(1)
    if chosen > 1:
        print("--value, --rule-type y --lock son mutuamente excluyentes: da solo uno.", file=sys.stderr)
        sys.exit(1)

    periods = _resolve_target_periods(db, sheet.id, args.period, args.from_period, args.to_period)

    rule = _build_rule_from_args(db, sheet.id, args) if has_rule else None
    value = Decimal(str(args.value)) if has_value else None
    user_id = args.user_id if args.user_id is not None else sheet.user_id

    results = _do_set_override(
        db, sheet.id, row, periods, value=value, rule=rule, lock=has_lock, note=args.note, created_by=user_id,
    )

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    _print_override_write_results(row, results, row_id_to_name)




def cmd_override_clear(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)
    periods = _resolve_target_periods(db, sheet.id, args.period, args.from_period, args.to_period)

    results = _do_clear_override(db, sheet.id, row, periods)

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    _print_override_clear_results(row, results, row_id_to_name)




def cmd_export(db, args) -> None:
    if args.mode == "formulas" and args.format == "csv":
        print("--mode formulas solo es válido con --format xlsx.", file=sys.stderr)
        sys.exit(1)
    if args.styled and args.format == "csv":
        print("--styled solo aplica a --format xlsx (un csv no tiene estilos).", file=sys.stderr)
        sys.exit(1)

    sheet = _pick_sheet(db, args.sheet_id)
    result = compute_sheet(sheet.id, db)
    all_periods = result["periods"]
    if not all_periods:
        print(f"La planilla #{sheet.id} no tiene períodos.", file=sys.stderr)
        sys.exit(1)

    # Sin --months/--before/--context: todo el rango (comportamiento historico,
    # util para exportar la planilla completa). Con cualquiera de los tres: la
    # misma ventana anclada en "hoy" que usa `show` (--months = N periodos
    # despues del actual, no "los primeros N por sort_order").
    if args.months is None and args.before is None and args.context is None:
        periods = all_periods
    else:
        anchor = find_anchor_period(all_periods)
        after = args.context if args.context is not None else (args.months if args.months is not None else 6)
        before = args.context if args.context is not None else (args.before if args.before is not None else 0)
        anchor_idx = all_periods.index(anchor)
        start = max(0, anchor_idx - before)
        end = min(len(all_periods), anchor_idx + after + 1)
        periods = all_periods[start:end]

    ext = "csv" if args.format == "csv" else "xlsx"
    if args.output:
        path = args.output
    else:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", sheet.name).strip("_").lower() or "flujo_de_caja"
        path = f"{slug}_{date.today().strftime('%Y%m%d')}.{ext}"

    if args.format == "csv":
        export_csv(sheet, result, periods, args.unit, args.show_ids, path)
    else:
        all_rows_by_id = {}
        for section in result["sections"]:
            for row_data in section["rows"]:
                all_rows_by_id[row_data["row"].id] = row_data["row"]
        export_xlsx(sheet, result, periods, all_rows_by_id, args.unit, args.mode, args.styled, args.show_ids, db, path)

    print(f"Exportado a {os.path.abspath(path)}  ({len(periods)} período(s), formato={args.format}, modo={args.mode})")




def cmd_available(db, args) -> None:
    """"Disponible para gastar ahora" = SALDO INICIAL del período actual,
    más el efecto neto en efectivo YA REALIZADO este período (cada fila con
    paid_value registrado, multiplicado por su signo efectivo hasta el
    resultado final -- ver _effective_sign_to_top), menos lo que ya está
    facturado pero aún sin pagar en filas de dirección "gasto" (se reserva,
    para no contar como disponible una plata que ya se sabe que hay que
    pagar).

    Antes esta cuenta era SALDO INICIAL menos el pending_value crudo
    (accrued-paid) de cada fila -- eso tenía un problema real: en cuanto una
    fila queda COMPLETAMENTE pagada (accrued == paid), su pending_value cae
    a 0 y desaparece de la resta, pero la plata que salió para pagarla NUNCA
    se había restado de la base -- el resultado no bajaba aunque ya se
    hubieran pagado facturas reales de este período (caso real: pagar el
    total facturado de una tarjeta no cambiaba el disponible en nada). Este
    cálculo usa paid_value (la plata que efectivamente se movió) para que
    "ya pagado" deje de comportarse igual que "todavía no ha pasado nada".
    """
    sheet = _pick_sheet(db, args.sheet_id)
    result = compute_sheet(sheet.id, db)
    periods = result["periods"]

    anchor = find_anchor_period(periods)
    if anchor is None:
        print(f"La planilla #{sheet.id} no tiene períodos.", file=sys.stderr)
        sys.exit(1)

    balance_row = _find_balance_row(db, sheet.id)
    try:
        rows_by_id, child_to_parent = _build_sum_rows_hierarchy(db, sheet.id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    balance_value = None
    realized_items: List[Tuple[str, Decimal]] = []  # ya ocurrido en efectivo (paid_value * signo)
    pending_items: List[Tuple[str, Decimal]] = []  # facturado, dirección gasto, aún sin pagar
    excluded_items: List[str] = []  # con datos reales pero sin ruta de agregación conocida

    for section in result["sections"]:
        for row_data in section["rows"]:
            row = row_data["row"]
            for cr in row_data["cells"]:
                if cr.period_id != anchor.id:
                    continue
                if row.id == balance_row.id:
                    balance_value = cr.projected_value
                    continue
                if row.row_type in _AGGREGATE_ROW_TYPES:
                    continue

                try:
                    multiplier = _effective_sign_to_top(row.id, rows_by_id, child_to_parent)
                except ValueError as e:
                    print(str(e), file=sys.stderr)
                    sys.exit(1)

                if multiplier is None:
                    if cr.paid_value is not None or cr.accrued_value is not None:
                        excluded_items.append(row.name)
                    continue

                if cr.paid_value is not None:
                    realized_items.append((row.name, multiplier * cr.paid_value))

                if multiplier < 0 and cr.accrued_value is not None:
                    reserved = cr.accrued_value - (cr.paid_value if cr.paid_value is not None else Decimal(0))
                    if reserved > 0:
                        pending_items.append((row.name, reserved))

    if balance_value is None:
        print(f"La fila '{balance_row.name}' no tiene valor para {_period_label(anchor)}.", file=sys.stderr)
        sys.exit(1)

    total_realized = sum((v for _, v in realized_items), Decimal(0))
    total_pending = sum((v for _, v in pending_items), Decimal(0))
    available = balance_value + total_realized - total_pending

    available_text = _fmt_number(available, args.unit)
    if available < 0:
        available_text = _c(available_text, "red")
    print(f"Disponible para gastar ahora ({_period_label(anchor)}): {available_text}")
    print(f"  {balance_row.name}: {_fmt_number(balance_value, args.unit)}")
    if realized_items:
        print("  Ya ocurrido este período (real, en efectivo):")
        for name, v in realized_items:
            sign = "+" if v >= 0 else "−"
            print(f"    {sign} {name}: {_fmt_number(abs(v), args.unit)}")
    if pending_items:
        print("  Facturado pero aún sin pagar (se reserva, no cuenta como disponible):")
        for name, v in pending_items:
            print(f"    − {name}: {_fmt_number(v, args.unit)}")
    if not realized_items and not pending_items:
        print("  (sin movimientos ni pendientes registrados este período todavía)")
    if excluded_items:
        print("  [aviso] excluidas del cálculo por no tener una ruta de suma hacia el resultado final:")
        for name in excluded_items:
            print(f"    {name}")


# _bridge_cards_for_period/_compute_real_column_values now live in
# backend/bridge_financing.py (imported above) -- draw_for_deficit needs
# the real SALDO FINAL too, to detect a deficit against reality instead
# of only the projected column.





def _print_close_report(report: CloseReport) -> None:
    if report.dry_run:
        print("[DRY RUN -- nada se guardó]")

    print(f"[OK] Cierre de {report.period_label} -- planilla #{report.sheet_id}")
    print(f"  {report.balance_row_name}: {_fmt_number(report.saldo_inicial_value, '1')}  (saldo inicial usado)")
    print(f"  Flujo neto real del período: {_fmt_number(report.real_net_flow, '1')}")
    print(
        f"  {report.balance_final_row_name}: {_fmt_number(report.saldo_final_value, '1')}  "
        f"(saldo final real, escrito como override)"
    )

    if report.rollovers:
        destino = report.next_period_label or "el período siguiente"
        print(f"  Arrastres hacia {destino}:")
        for r in report.rollovers:
            print(f"    - {r.row_name}: pendiente {_fmt_number(r.pending_amount, '1')} -- {r.disposition}")
    else:
        print("  (sin arrastres: todo lo devengado este período quedó pagado)")

    if report.warnings:
        print("  [aviso] filas con proyección pero sin ningún dato real registrado (no se asumieron pendientes):")
        for name in report.warnings:
            print(f"    - {name}")




def cmd_period_close(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)

    period_date = _parse_base_period(args.period)
    period_dt = datetime(period_date.year, period_date.month, 1)
    period = db.query(SheetPeriod).filter(
        SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == period_dt,
    ).first()
    if not period:
        print(f"La planilla #{sheet.id} no tiene un período para {period_dt.strftime('%Y-%m')}.", file=sys.stderr)
        sys.exit(1)

    balance_row = _resolve_row(db, sheet.id, args.balance_row) if args.balance_row else None
    user_id = args.user_id if args.user_id is not None else sheet.user_id

    try:
        report = close_period(
            db, sheet, period, user_id,
            balance_row=balance_row,
            assume_unrecorded_as_pending=args.assume_unrecorded_as_pending,
            dry_run=args.dry_run,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    _print_close_report(report)


# ---------------------------------------------------------------------------
# creditcard: card / charge / statement / sync
# ---------------------------------------------------------------------------
#
# Every write below goes through backend.creditcard_statements (card/charge/
# statement resolution and the sheet-cell sync itself) -- this section is
# purely CLI plumbing: parse the typed flags, call the library function, and
# print a clear confirmation. resolve_credit_card/compute_instant_statement/
# sync_period never touch sys.exit -- they raise ValueError, which every
# cmd_creditcard_* function here catches and turns into stderr + sys.exit(1),
# exactly like every other error in this CLI.



def _parse_yyyy_mm_dd(value: str, flag_name: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        print(f"{flag_name} '{value}' no tiene el formato YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)




def resolve_wallet(db, user_id_or_none: Optional[int], wallet_arg: str) -> Wallet:
    """Resolve `wallet_arg` (numeric id, exact name, or substring) to a
    Wallet, optionally scoped to one user -- same id/exact/substring
    matching resolve_credit_card uses, raising ValueError (never sys.exit,
    this is library-shaped) listing every candidate on ambiguity."""
    query = db.query(Wallet)
    if user_id_or_none is not None:
        query = query.filter(Wallet.user_id == user_id_or_none)
    scope_note = f" del usuario #{user_id_or_none}" if user_id_or_none is not None else ""

    if wallet_arg.isdigit():
        wallet = query.filter(Wallet.id == int(wallet_arg)).first()
        if wallet is None:
            raise ValueError(f"No existe la billetera #{wallet_arg}{scope_note}.")
        return wallet

    wallets = query.all()
    needle_cf = wallet_arg.casefold()
    exact = [w for w in wallets if w.name.casefold() == needle_cf]
    matches = exact or [w for w in wallets if needle_cf in w.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listing = "; ".join(f"[{w.id}] {w.name}" for w in matches)
        raise ValueError(f"'{wallet_arg}' es ambiguo, coincide con {len(matches)} billeteras{scope_note}: {listing}")
    raise ValueError(f"No se encontró ninguna billetera que coincida con '{wallet_arg}'{scope_note}.")




def _resolve_wallet_or_exit(db, wallet_arg: str) -> Wallet:
    try:
        return resolve_wallet(db, None, wallet_arg)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)




def cmd_wallet_list(db, args) -> None:
    query = db.query(Wallet)
    if args.user_id is not None:
        query = query.filter(Wallet.user_id == args.user_id)
    wallets = query.order_by(Wallet.id).all()
    if not wallets:
        print("No hay billeteras registradas.")
        return
    for w in wallets:
        desc = f"  ({w.description})" if w.description else ""
        print(
            f"[{w.id}] {w.name}  tipo={w.wallet_type}  "
            f"saldo={_fmt_number(Decimal(str(w.balance)), '1')} {w.currency}  "
            f"usuario_id={w.user_id}{desc}"
        )




def _do_edit_wallet(
    db, wallet: Wallet, *, name=_UNSET, wallet_type=_UNSET, currency=_UNSET, balance=_UNSET, description=_UNSET,
) -> Dict[str, Tuple]:
    """Partial update, same _UNSET-sentinel/only-report-actual-changes
    pattern as _do_edit_card/_do_edit_row."""
    fields = {"name": name, "wallet_type": wallet_type, "currency": currency, "balance": balance,
              "description": description}
    changes: Dict[str, Tuple] = {}
    for field_name, value in fields.items():
        if value is _UNSET:
            continue
        old = getattr(wallet, field_name)
        if value != old:
            changes[field_name] = (old, value)
            setattr(wallet, field_name, value)
    if changes:
        db.commit()
        db.refresh(wallet)
    return changes




def cmd_wallet_edit(db, args) -> None:
    wallet = _resolve_wallet_or_exit(db, args.wallet)

    kwargs = {}
    if args.name is not None:
        kwargs["name"] = args.name
    if args.type is not None:
        kwargs["wallet_type"] = args.type
    if args.currency is not None:
        kwargs["currency"] = args.currency.upper()
    if args.balance is not None:
        kwargs["balance"] = args.balance
    if args.description is not None:
        kwargs["description"] = args.description

    changes = _do_edit_wallet(db, wallet, **kwargs)
    if not changes:
        print(f"Billetera [{wallet.id}] '{wallet.name}': no se pasó ningún cambio.")
        return

    print(f"[OK] Billetera [{wallet.id}] '{wallet.name}' actualizada:")
    for field, (old, new) in changes.items():
        old_text = _fmt_number(Decimal(str(old)), "1") if field == "balance" else old
        new_text = _fmt_number(Decimal(str(new)), "1") if field == "balance" else new
        print(f"  {field}: {old_text}  ->  {new_text}")


# ---------------------------------------------------------------------------
# wallet movement add/list/undo -- Wallets Phase 2: a real cash event that
# credits/debits a Wallet AND records a sheet row as real, in one write. The
# actual logic lives in backend/wallet_movements.py, same split as
# period_close.py/creditcard_statements.py/bridge_financing.py; cli.py only
# resolves CLI args into objects and reports the result.
# ---------------------------------------------------------------------------



def cmd_wallet_movement_add(db, args) -> None:
    wallet = _resolve_wallet_or_exit(db, args.wallet)
    sheet = _pick_sheet(db, args.sheet_id)
    row = _resolve_row(db, sheet.id, args.row)

    period_date = _parse_base_period(args.period) if args.period else date.today().replace(day=1)
    period_dt = datetime(period_date.year, period_date.month, 1)
    period = db.query(SheetPeriod).filter(
        SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == period_dt,
    ).first()
    if not period:
        print(f"La planilla #{sheet.id} no tiene un período para {period_dt.strftime('%Y-%m')}.", file=sys.stderr)
        sys.exit(1)

    user_id = args.user_id if args.user_id is not None else sheet.user_id
    try:
        result = do_wallet_movement_add(
            db, wallet, sheet.id, row, period, Decimal(str(args.amount)), note=args.note, created_by=user_id,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    m = result.movement
    sign_text = "+" if m.amount >= 0 else "-"
    print(
        f"[OK] Movimiento #{m.id} en billetera [{wallet.id}] {wallet.name}: "
        f"{sign_text}{_fmt_number(Decimal(str(abs(m.amount))), '1')} {wallet.currency}  "
        f"(saldo {_fmt_number(Decimal(str(result.wallet_balance_before)), '1')} -> "
        f"{_fmt_number(Decimal(str(result.wallet_balance_after)), '1')})"
    )
    before_text = _fmt_number(result.cell_paid_before, "1") if result.cell_paid_before is not None else "—"
    print(
        f"  '{row.name}' — {_period_label(period)}: paid {before_text} -> "
        f"{_fmt_number(result.cell_paid_after, '1')}"
    )




def cmd_wallet_movement_undo(db, args) -> None:
    movement = db.query(WalletMovement).filter(WalletMovement.id == args.movement_id).first()
    if movement is None:
        print(f"No existe el movimiento #{args.movement_id}.", file=sys.stderr)
        sys.exit(1)
    wallet = db.query(Wallet).filter(Wallet.id == movement.wallet_id).first()
    user_id = args.user_id if args.user_id is not None else (wallet.user_id if wallet else None)

    try:
        result = do_wallet_movement_undo(db, movement, note=args.note, created_by=user_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(
        f"[OK] Se deshizo el movimiento #{movement.id} (billetera [{wallet.id}] {wallet.name}): "
        f"saldo {_fmt_number(Decimal(str(result.wallet_balance_before)), '1')} -> "
        f"{_fmt_number(Decimal(str(result.wallet_balance_after)), '1')}  "
        f"(reversión registrada como movimiento #{result.reversal.id})"
    )




def _wallets_summary(db, user_id: int) -> List[Tuple[str, Decimal, List[Wallet]]]:
    """(currency, total, wallets) per currency the user holds -- grouped,
    never silently summed across currencies (a CLP total plus a USD total
    would be a meaningless number, not "more available")."""
    wallets = db.query(Wallet).filter(Wallet.user_id == user_id).order_by(Wallet.id).all()
    by_currency: Dict[str, List[Wallet]] = {}
    for w in wallets:
        by_currency.setdefault(w.currency, []).append(w)
    return [
        (currency, sum((Decimal(str(w.balance)) for w in group), Decimal(0)), group)
        for currency, group in by_currency.items()
    ]




def _wallets_total_for_currency(db, user_id: int, sheet_currency: str) -> Optional[Decimal]:
    """The ONE wallets total in `sheet_currency` specifically -- what the
    inline SALDO INICIAL parenthetical / the combined total can safely add
    to a credit total, without silently mixing currencies. None if the
    user holds no wallet in that currency (they may still hold others)."""
    for currency, total, _wallets in _wallets_summary(db, user_id):
        if currency == sheet_currency:
            return total
    return None




def _print_wallets_section(db, user_id: int, unit: str) -> None:
    groups = _wallets_summary(db, user_id)
    if not groups:
        return
    print(_c("BILLETERAS", "bold"))
    for currency, total, wallets in groups:
        print(f"  Total {currency}: {_fmt_number(total, unit)}")
        for w in wallets:
            desc = f"  ({w.description})" if w.description else ""
            print(f"    [{w.id}] {w.name} ({w.wallet_type}): {_fmt_number(Decimal(str(w.balance)), unit)}{desc}")
    print()




_WIZARD_RULE_MENU = [
    ("constant", "Valor constante"),
    ("previous_period", "Igual al período anterior"),
    ("sum_rows", "Suma de otras filas"),
    ("percent_of_row", "Porcentaje de otra fila"),
    ("rolling_average", "Promedio móvil"),
    ("carry_forward", "Arrastre (carry forward) sobre otra regla"),
]

_SECTION_TYPE_CHOICES = ["income", "expense", "financing", "balance", "custom"]


def _wizard_prompt_choice(prompt: str, choices: List[str], default: str) -> str:
    """Prompt with a fixed set of valid answers; blank -> default, invalid ->
    reprompt with an error (never guesses which one was meant)."""
    choices_text = "/".join(choices)
    while True:
        raw = input(f"{prompt} ({choices_text}) [{default}]: ").strip()
        if not raw:
            return default
        if raw in choices:
            return raw
        print(f"  Valor inválido. Opciones: {choices_text}.")




def _wizard_prompt_int(prompt: str, default: int, min_value: int = 1) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("  Debe ser un número entero.")
            continue
        if value < min_value:
            print(f"  Debe ser >= {min_value}.")
            continue
        return value




def _wizard_prompt_period(prompt: str, default: date) -> date:
    while True:
        raw = input(f"{prompt} [{default.strftime('%Y-%m')}]: ").strip()
        if not raw:
            return default
        try:
            return _parse_base_period(raw)
        except Exception:
            print("  Formato inválido, usa YYYY-MM.")




def _wizard_prompt_period_or_range(db, sheet_id: int) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (period, from_period, to_period) for _resolve_target_periods
    -- either the first is set xor the last two are, or all three are None
    meaning "cancelled". Doesn't validate the period(s) exist yet -- the
    caller passes these straight to _resolve_target_periods for that."""
    single = input("  Período único (YYYY-MM), o enter para dar un rango: ").strip()
    if single:
        return single, None, None
    from_period = input("  Desde (YYYY-MM): ").strip()
    to_period = input("  Hasta (YYYY-MM): ").strip()
    if not from_period or not to_period:
        print("  Cancelado (rango incompleto).")
        return None, None, None
    return None, from_period, to_period




def _wizard_list_rows(rows: List[SheetRow]) -> None:
    for i, r in enumerate(rows, 1):
        print(f"      {i}) [{r.id}] {r.section.name} > {r.name}")




def _wizard_pick_row_optional(rows: List[SheetRow], prompt: str) -> Optional[SheetRow]:
    """Blank or invalid -> None (caller treats None as "no reference given",
    never an error -- used for previous_period's optional row)."""
    if not rows:
        return None
    _wizard_list_rows(rows)
    choice = input(prompt).strip()
    if not choice:
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1]
    print("  Selección inválida -- se usa 'esta misma fila'.")
    return None




def _wizard_pick_row_required(rows: List[SheetRow], prompt: str) -> Optional[SheetRow]:
    if not rows:
        print("    No hay filas creadas todavía para referenciar.")
        return None
    _wizard_list_rows(rows)
    choice = input(prompt).strip()
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        return rows[int(choice) - 1]
    print("    Selección inválida.")
    return None




def _wizard_pick_rows_multi(rows: List[SheetRow], prompt: str) -> List[SheetRow]:
    if not rows:
        print("    No hay filas creadas todavía para referenciar.")
        return []
    _wizard_list_rows(rows)
    choice = input(prompt).strip()
    if not choice:
        return []
    picked: List[SheetRow] = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(rows):
            picked.append(rows[int(part) - 1])
        else:
            print(f"    '{part}' no es una opción válida -- se ignora.")
    return picked




def _wizard_build_rule(db, sheet_id: int, rule_type: str, rows: List[SheetRow]) -> Optional[dict]:
    """Prompt for the typed fields _build_rule_from_args needs for
    `rule_type`, referencing rows only from `rows` (the ones the caller
    considers "already known" -- see cmd_wizard_new/cmd_wizard_edit for what
    that list is in each context), then delegate to _build_rule_from_args
    itself for validation and JSON-shape construction -- never duplicated
    here. Returns None ("no rule") if the user cancels or a required field
    is left blank -- an interactive session shouldn't die over that, it
    should just leave the row without a rule for now (fixable via
    `row edit` / the wizard's "editar fila" option)."""
    ns = SimpleNamespace(
        rule_type=rule_type, rule_value=None, rule_row=None, rule_rows=None,
        rule_percent=None, rule_n=None, rule_base_type=None, rule_base_value=None, rule_base_rows=None,
    )

    if rule_type == "constant":
        ns.rule_value = input("    Valor constante: ").strip()
        if not ns.rule_value:
            print("    Se requiere un valor -- se cancela la regla.")
            return None

    elif rule_type == "previous_period":
        print("    Fila de referencia (enter = esta misma fila):")
        row = _wizard_pick_row_optional(rows, "    Número, o enter para 'esta misma fila': ")
        if row is not None:
            ns.rule_row = str(row.id)

    elif rule_type == "sum_rows":
        print("    Filas a sumar:")
        picked = _wizard_pick_rows_multi(rows, "    Números separados por coma: ")
        if not picked:
            print("    sum_rows requiere al menos una fila ya creada -- se cancela la regla.")
            return None
        ns.rule_rows = ",".join(str(r.id) for r in picked)

    elif rule_type == "percent_of_row":
        print("    Fila de referencia:")
        row = _wizard_pick_row_required(rows, "    Número: ")
        if row is None:
            return None
        ns.rule_row = str(row.id)
        ns.rule_percent = input("    Porcentaje: ").strip()
        if not ns.rule_percent:
            print("    Se requiere un porcentaje -- se cancela la regla.")
            return None

    elif rule_type == "rolling_average":
        raw_n = input("    Cantidad de períodos: ").strip()
        if not raw_n.isdigit():
            print("    Se requiere un número entero -- se cancela la regla.")
            return None
        ns.rule_n = int(raw_n)

    elif rule_type == "carry_forward":
        base_choice = input("    Base del arrastre: 1) constante  2) suma de filas [1]: ").strip() or "1"
        if base_choice == "2":
            print("    Filas a sumar (base):")
            picked = _wizard_pick_rows_multi(rows, "    Números separados por coma: ")
            if not picked:
                print("    sum_rows requiere al menos una fila ya creada -- se cancela la regla.")
                return None
            ns.rule_base_type = "sum_rows"
            ns.rule_base_rows = ",".join(str(r.id) for r in picked)
        else:
            ns.rule_base_type = "constant"
            ns.rule_base_value = input("    Valor constante base: ").strip()
            if not ns.rule_base_value:
                print("    Se requiere un valor -- se cancela la regla.")
                return None

    try:
        return _build_rule_from_args(db, sheet_id, ns)
    except SystemExit:
        print(
            "    No se pudo construir la regla con esos datos -- la fila queda sin regla "
            "(puedes agregarla después con 'row edit' o desde el wizard de edición)."
        )
        return None




def _wizard_prompt_rule_for_new_row(db, sheet_id: int, rows: List[SheetRow]) -> Optional[dict]:
    """Numbered menu of the 6 rule types (never raw JSON), plus "sin regla".
    `rows` is the set of rows a reference-taking rule type may pick from --
    for `wizard new` that's only rows already created earlier in this same
    wizard session (can't reference a row that doesn't exist yet)."""
    print("    Regla de proyección:")
    print("      0) Sin regla")
    for i, (_type, label) in enumerate(_WIZARD_RULE_MENU, 1):
        print(f"      {i}) {label}")
    choice = input("    Elige una opción [0]: ").strip() or "0"
    if choice == "0":
        return None
    if not choice.isdigit() or not (1 <= int(choice) <= len(_WIZARD_RULE_MENU)):
        print("    Opción inválida -- la fila queda sin regla.")
        return None
    rule_type = _WIZARD_RULE_MENU[int(choice) - 1][0]
    return _wizard_build_rule(db, sheet_id, rule_type, rows)




def _wizard_prompt_rule_for_edit(db, sheet_id: int, rows: List[SheetRow]):
    """Same menu as _wizard_prompt_rule_for_new_row, plus "no cambiar" (the
    default) and "quitar regla" -- returns _UNSET/None/dict for
    _do_edit_row's rule= kwarg semantics directly."""
    print("    Regla de proyección:")
    print("      0) No cambiar")
    print("      1) Quitar regla")
    for i, (_type, label) in enumerate(_WIZARD_RULE_MENU, 2):
        print(f"      {i}) {label}")
    choice = input("    Elige una opción [0]: ").strip() or "0"
    if choice == "0":
        return _UNSET
    if choice == "1":
        return None
    if not choice.isdigit() or not (2 <= int(choice) < 2 + len(_WIZARD_RULE_MENU)):
        print("    Opción inválida -- no se cambia la regla.")
        return _UNSET
    rule_type = _WIZARD_RULE_MENU[int(choice) - 2][0]
    return _wizard_build_rule(db, sheet_id, rule_type, rows)




def _wizard_maybe_set_end_date(db, sheet: CashflowSheet, row: SheetRow, owner_user_id: int) -> None:
    """Step 4 of `wizard new`'s row loop (also offered from the wizard-edit
    'agregar fila' action): a row can have a known end date. If given,
    forces the row to 0 from the month right after it through the sheet's
    last generated period, via the SAME _do_set_override the `override set`
    command uses -- an ordinary, reversible override, never a special row
    state. The note explains this explicitly, and is printed to the user too."""
    raw = input("  ¿Esta fila tiene una fecha de término conocida? (mes YYYY-MM, o enter si no): ").strip()
    if not raw:
        return
    try:
        end_date = _parse_base_period(raw)
    except Exception:
        print(f"  [aviso] '{raw}' no es un mes válido (formato YYYY-MM) -- se omite la fecha de término.")
        return

    all_periods = sorted(sheet.periods, key=lambda p: p.sort_order)
    if not all_periods:
        print("  [aviso] la planilla no tiene períodos todavía -- no se pudo aplicar la fecha de término.")
        return
    last_period = all_periods[-1]

    y, m = end_date.year, end_date.month
    m += 1
    if m == 13:
        m = 1
        y += 1
    from_period_str = f"{y:04d}-{m:02d}"
    to_period_str = last_period.period_date.strftime("%Y-%m")

    if _parse_base_period(from_period_str) > _parse_base_period(to_period_str):
        print(
            f"  '{row.name}' termina en el último período de la planilla ({to_period_str}): "
            f"no hay meses posteriores que forzar a 0."
        )
        return

    try:
        periods = _resolve_target_periods(db, sheet.id, None, from_period_str, to_period_str)
    except SystemExit:
        return

    note = (
        f"Fin de fila detectado por el wizard: '{row.name}' termina en {raw}, valor forzado a 0 "
        f"desde {from_period_str} hasta {to_period_str}. Esto es solo un override -- no un estado "
        f"especial de la fila -- y es reversible con 'override clear'."
    )
    results = _do_set_override(
        db, sheet.id, row, periods, value=Decimal("0"), rule=None, lock=False, note=note, created_by=owner_user_id,
    )
    print(f"  [OK] {note}")
    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    _print_override_write_results(row, results, row_id_to_name, indent="    ")




def _wizard_add_row_named(
    db, sheet: CashflowSheet, section: SheetSection, name: str, created_rows: List[SheetRow], owner_user_id: int,
) -> SheetRow:
    row_type = _wizard_prompt_choice("    Tipo de fila", _ROW_TYPE_CHOICES, "input")
    print("    Nota: el signo solo importa si esta fila es referenciada dentro del sum_rows de otra fila.")
    sign = _wizard_prompt_choice("    Signo", ["positive", "negative"], "positive")
    rule = _wizard_prompt_rule_for_new_row(db, sheet.id, created_rows)

    row = _do_add_row(db, section, name, row_type, sign, rule)
    created_rows.append(row)

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    print(
        f"    [OK] Fila creada: [{row.id}] {section.name} > {row.name}  tipo={row.row_type}  "
        f"signo={row.sign}  regla: {_describe_rule(row.default_projection_rule, row_id_to_name)}"
    )

    _wizard_maybe_set_end_date(db, sheet, row, owner_user_id)
    return row




def _wizard_rows_for_section(
    db, sheet: CashflowSheet, section: SheetSection, created_rows: List[SheetRow], owner_user_id: int,
) -> None:
    print(f"\n-- Filas para la sección '{section.name}' --")
    while True:
        name = input("  ¿Otra fila en esta sección? (nombre, o enter para terminar): ").strip()
        if not name or name.casefold() == "listo":
            break
        _wizard_add_row_named(db, sheet, section, name, created_rows, owner_user_id)




def _wizard_edit_add_section(db, sheet: CashflowSheet) -> None:
    name = input("  Nombre de la sección: ").strip()
    if not name:
        print("  Cancelado (nombre vacío).")
        return
    section_type = _wizard_prompt_choice("  Tipo de sección", _SECTION_TYPE_CHOICES, "custom")
    section = _do_add_section(db, sheet, name, section_type)
    print(f"  [OK] Sección creada: [{section.id}] {section.name}  tipo={section.section_type}  orden={section.sort_order}")




def _wizard_edit_add_row(db, sheet: CashflowSheet) -> None:
    section_arg = input("  Sección (nombre o id): ").strip()
    if not section_arg:
        print("  Cancelado.")
        return
    section = _wizard_resolve_section(db, sheet.id, section_arg)
    if section is None:
        return

    name = input("  Nombre de la fila: ").strip()
    if not name:
        print("  Cancelado (nombre vacío).")
        return

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_type = _wizard_prompt_choice("  Tipo de fila", _ROW_TYPE_CHOICES, "input")
    print("  Nota: el signo solo importa si esta fila es referenciada dentro del sum_rows de otra fila.")
    sign = _wizard_prompt_choice("  Signo", ["positive", "negative"], "positive")
    rule = _wizard_prompt_rule_for_new_row(db, sheet.id, all_rows)

    row = _do_add_row(db, section, name, row_type, sign, rule)
    row_id_to_name = {r.id: r.name for r in all_rows}
    row_id_to_name[row.id] = row.name
    print(
        f"  [OK] Fila creada: [{row.id}] {section.name} > {row.name}  tipo={row.row_type}  "
        f"signo={row.sign}  regla: {_describe_rule(row.default_projection_rule, row_id_to_name)}"
    )

    _wizard_maybe_set_end_date(db, sheet, row, sheet.user_id)




def _wizard_edit_row(db, sheet: CashflowSheet) -> None:
    row_arg = input("  Fila a editar (nombre o id): ").strip()
    if not row_arg:
        print("  Cancelado.")
        return
    row = _wizard_resolve_row(db, sheet.id, row_arg)
    if row is None:
        return

    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}

    print(f"  Editando [{row.id}] {row.section.name} > {row.name} (enter = no cambiar ese campo)")
    new_name = input(f"  Nombre [{row.name}]: ").strip()
    new_row_type_raw = input(f"  Tipo de fila [{row.row_type}] ({'/'.join(_ROW_TYPE_CHOICES)}): ").strip()
    new_sign_raw = input(f"  Signo [{row.sign}] (positive/negative): ").strip()
    rule_choice = _wizard_prompt_rule_for_edit(db, sheet.id, all_rows)

    kwargs = {}
    if new_name:
        kwargs["name"] = new_name
    if new_row_type_raw:
        if new_row_type_raw in _ROW_TYPE_CHOICES:
            kwargs["row_type"] = new_row_type_raw
        else:
            print(f"  '{new_row_type_raw}' no es un tipo válido -- no se cambia el tipo de fila.")
    if new_sign_raw:
        if new_sign_raw in ("positive", "negative"):
            kwargs["sign"] = new_sign_raw
        else:
            print(f"  '{new_sign_raw}' no es un signo válido -- no se cambia el signo.")
    if rule_choice is not _UNSET:
        kwargs["rule"] = rule_choice

    changes = _do_edit_row(db, row, **kwargs)
    if not changes:
        print(f"  Fila [{row.id}] '{row.name}': no se pasó ningún cambio.")
        return

    print(f"  [OK] Fila [{row.id}] '{row.name}' actualizada:")
    for field, (old, new) in changes.items():
        if field == "rule":
            print(f"    regla: {_describe_rule(old, row_id_to_name)}  ->  {_describe_rule(new, row_id_to_name)}")
        else:
            print(f"    {field}: {old!r}  ->  {new!r}")




def _wizard_edit_set_override(db, sheet: CashflowSheet) -> None:
    row_arg = input("  Fila (nombre o id): ").strip()
    if not row_arg:
        print("  Cancelado.")
        return
    row = _wizard_resolve_row(db, sheet.id, row_arg)
    if row is None:
        return

    period, from_period, to_period = _wizard_prompt_period_or_range(db, sheet.id)
    if period is None and from_period is None:
        return
    try:
        periods = _resolve_target_periods(db, sheet.id, period, from_period, to_period)
    except SystemExit:
        return

    print("  Valor a escribir:")
    print("    1) Valor fijo")
    print("    2) Regla")
    print("    3) Lock (congela el valor efectivo actual)")
    kind = _wizard_prompt_choice("  Elige una opción", ["1", "2", "3"], "1")

    value = None
    rule = None
    lock = False
    if kind == "1":
        raw_value = input("    Valor: ").strip()
        if not raw_value:
            print("  Cancelado (valor vacío).")
            return
        try:
            value = Decimal(raw_value)
        except Exception:
            print(f"  '{raw_value}' no es un número válido -- cancelado.")
            return
    elif kind == "2":
        all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
        rule = _wizard_prompt_rule_for_new_row(db, sheet.id, all_rows)
        if rule is None:
            print("  Cancelado (no se definió ninguna regla).")
            return
    else:
        lock = True

    note = input("  Nota (opcional): ").strip() or None

    results = _do_set_override(
        db, sheet.id, row, periods, value=value, rule=rule, lock=lock, note=note, created_by=sheet.user_id,
    )
    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    _print_override_write_results(row, results, row_id_to_name, indent="  ")




def _wizard_edit_clear_override(db, sheet: CashflowSheet) -> None:
    row_arg = input("  Fila (nombre o id): ").strip()
    if not row_arg:
        print("  Cancelado.")
        return
    row = _wizard_resolve_row(db, sheet.id, row_arg)
    if row is None:
        return

    period, from_period, to_period = _wizard_prompt_period_or_range(db, sheet.id)
    if period is None and from_period is None:
        return
    try:
        periods = _resolve_target_periods(db, sheet.id, period, from_period, to_period)
    except SystemExit:
        return

    results = _do_clear_override(db, sheet.id, row, periods)
    all_rows = db.query(SheetRow).join(SheetSection).filter(SheetSection.sheet_id == sheet.id).all()
    row_id_to_name = {r.id: r.name for r in all_rows}
    _print_override_clear_results(row, results, row_id_to_name, indent="  ")




def cmd_wizard_edit(db, args) -> None:
    sheet = _pick_sheet(db, args.sheet_id)
    print(f"=== Asistente: editar planilla #{sheet.id} (\"{sheet.name}\") ===")

    while True:
        print("\n1) Agregar sección")
        print("2) Agregar fila")
        print("3) Editar fila")
        print("4) Escribir override")
        print("5) Borrar override")
        print("6) Salir")
        choice = input("Elige una opción: ").strip()

        if choice == "1":
            _wizard_edit_add_section(db, sheet)
        elif choice == "2":
            _wizard_edit_add_row(db, sheet)
        elif choice == "3":
            _wizard_edit_row(db, sheet)
        elif choice == "4":
            _wizard_edit_set_override(db, sheet)
        elif choice == "5":
            _wizard_edit_clear_override(db, sheet)
        elif choice == "6" or choice.casefold() in ("salir", "q", "exit"):
            print("Hasta luego.")
            break
        else:
            print("Opción inválida.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

