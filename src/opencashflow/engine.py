"""Cashflow sheet calculation engine.

Computes projected values for all cells of a sheet using the following
priority order per cell:

  1. Active CellOverride with override_type = 'manual_value'  → use override.value
  2. Active CellOverride with override_type = 'manual_rule'   → evaluate custom_rule
  3. Active CellOverride with override_type = 'lock'          → use override.value
                          (frozen at whatever was captured when the lock was
                          created — the engine only reads it, never captures
                          it; see the consuming app's override write path)
  4. SheetRow.default_projection_rule                         → evaluate row rule
  5. No rule                                                  → None (empty)

Supported rules in V1:
  - constant          { "type": "constant", "value": <number> }
  - previous_period   { "type": "previous_period" }
                      { "type": "previous_period", "row_id": <int> }
                        Reads ANOTHER row's value in the previous period
                        (defaults to this row when "row_id" is omitted).
                        This is what makes a running balance possible:
                          Saldo Inicial[t] = previous_period(row_id=Saldo Final)
                          Saldo Final[t]   = sum_rows([Saldo Inicial, Flujo Neto])
                        NOTE: this NEVER creates a same-period dependency
                        (see _extract_same_period_deps) — a previous_period
                        reference, even with row_id, always resolves against
                        the fully-computed prior period, so it cannot
                        participate in an intra-period cycle. Do not add it
                        to the same-period adjacency graph.
  - sum_rows          { "type": "sum_rows", "row_ids": [<int>, ...] }
                        Sums the same-period value of each listed row.
                        Each operand is multiplied by the SIGN of its OWN
                        row (SheetRow.sign: "positive" → +1, "negative" → -1)
                        before summing. This lets amounts stay stored as
                        positive magnitudes while still netting correctly,
                        e.g. Flujo Neto = sum_rows([Total Ingresos (positive),
                        Total Egresos (negative)]).
  - percent_of_row    { "type": "percent_of_row", "row_id": <int>, "percent": <number> }
                        value = referenced row's same-period value * percent / 100.
                        Same-period reference → DOES create a dependency edge
                        and participates in cycle detection (unlike
                        previous_period).
  - rolling_average   { "type": "rolling_average", "n": <int> }
                        Average of this row's own value over the last N
                        periods (walking backward from the period being
                        evaluated). Periods with no value are skipped —
                        both from the sum and from the count — rather than
                        counted as 0, so a short history doesn't drag the
                        average down artificially. Resolves to None (empty)
                        if none of the last N periods have a value, e.g. at
                        the very start of a sheet with no history at all.
                        Like previous_period, this NEVER creates a
                        same-period dependency — it only ever reads
                        already-resolved prior periods, so it cannot
                        participate in an intra-period cycle.
  - carry_forward     { "type": "carry_forward", "base_rule": { ... } }
                        value = evaluate(base_rule, this period)
                                + max(0, this row's own pending_value in the
                                  PREVIOUS period), where pending_value is
                                  accrued_value - paid_value (only when BOTH
                                  are present on that cell — see
                                  _real_fields; None otherwise, and a None
                                  pending contributes 0, same as "nothing to
                                  carry"). Only a positive pending amount is
                                  carried forward — an over-payment (negative
                                  pending) is never subtracted back out.
                        If there is no previous period at all (first period
                        in sort order), the carried amount is 0. If the
                        base_rule has no data AND there's nothing positive
                        to carry, the whole cell resolves to None (empty) —
                        exactly like every other "no data" case in this
                        engine; it only ever returns a value when at least
                        one side actually contributed something.
                        base_rule may be any other rule type EXCEPT
                        carry_forward itself — nesting is rejected with
                        error="unsupported_rule:carry_forward(nested)"
                        rather than recursing (there's no legitimate use
                        case, and it would need extra cycle bookkeeping this
                        design doesn't cover).
                        Like previous_period and rolling_average, the
                        carry-forward reach into the previous period NEVER
                        creates a same-period dependency by itself — only
                        base_rule's own same-period references (e.g. if
                        base_rule is sum_rows or percent_of_row) do, and
                        those are still picked up by
                        _extract_same_period_deps recursing into base_rule.

Rules deferred to later iterations:
  - running_balance, ledger_aggregate
  Any other/unknown rule type resolves to None (effective_source="empty")
  AND sets error="unsupported_rule:<type>" on the cell, so it is
  distinguishable from a row that was intentionally left without a rule.

Cycle detection:
  Before evaluating, the engine builds the intra-period dependency graph
  (only same-period rules — sum_rows and percent_of_row — can create
  cycles; previous_period never does, even when it references another
  row via "row_id"). It runs a DFS topological sort and marks any cell
  participating in a cycle with error='cycle_detected'.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from opencashflow.models import CashflowSheet, SheetCell, SheetPeriod, SheetRow, SheetSection

MAX_DEPENDENCY_DEPTH = 20


class EffectiveSource(str, Enum):
    MANUAL = "manual"
    RULE = "rule"
    LEDGER = "ledger"
    EMPTY = "empty"


@dataclass
class CellResult:
    row_id: int
    period_id: int
    projected_value: Optional[Decimal]
    actual_value: Optional[Decimal] = None
    accrued_value: Optional[Decimal] = None
    paid_value: Optional[Decimal] = None
    pending_value: Optional[Decimal] = None
    variance: Optional[Decimal] = None
    effective_source: Optional[str] = None
    error: Optional[str] = None


def _get_active_override(cell_overrides: list) -> Optional[Any]:
    """Return the active (not superseded) override for a cell, or None."""
    for ov in reversed(cell_overrides):
        if ov.superseded_at is None:
            return ov
    return None


def _extract_same_period_deps(rule: Optional[Dict]) -> List[int]:
    """Return list of row_ids that this rule depends on in the SAME period.

    IMPORTANT: "previous_period" (even with an explicit "row_id") is
    intentionally NOT included here. It always reads an already-fully-
    resolved prior period, so it can never participate in an intra-period
    cycle. Adding it to this function would make _detect_cycles falsely
    flag any running-balance pair (e.g. Saldo Inicial / Saldo Final) as
    cyclic and blank both rows in every period.
    """
    if not rule:
        return []
    rule_type = rule.get("type")
    if rule_type == "sum_rows":
        return list(rule.get("row_ids", []))
    if rule_type == "percent_of_row":
        row_id = rule.get("row_id")
        return [row_id] if row_id is not None else []
    if rule_type == "carry_forward":
        # carry_forward's OWN cross-period read (this row's pending_value in
        # the previous period) never creates a dependency, exactly like
        # previous_period/rolling_average. But its base_rule might (e.g. a
        # sum_rows or percent_of_row base_rule) — those deps still need to
        # be registered so cycle detection sees them.
        return _extract_same_period_deps(rule.get("base_rule"))
    return []


def _detect_cycles(
    row_ids: List[int],
    adjacency: Dict[int, List[int]],
) -> Set[int]:
    """Return the set of row_ids involved in a cycle (DFS coloring).

    Colors: 0=unvisited, 1=in-progress, 2=done
    """
    color: Dict[int, int] = {rid: 0 for rid in row_ids}
    in_cycle: Set[int] = set()

    def dfs(node: int, path: List[int]) -> bool:
        if color[node] == 2:
            return False
        if color[node] == 1:
            # Found a cycle — mark all nodes in the current path that are in the cycle
            cycle_start = path.index(node)
            for n in path[cycle_start:]:
                in_cycle.add(n)
            return True
        color[node] = 1
        path.append(node)
        for neighbor in adjacency.get(node, []):
            if neighbor in color:
                dfs(neighbor, path)
        path.pop()
        color[node] = 2
        return False

    for rid in row_ids:
        if color[rid] == 0:
            dfs(rid, [])

    return in_cycle


def _real_fields(cell: Optional[SheetCell], projected_value: Optional[Decimal]) -> Dict[str, Optional[Decimal]]:
    """Pass through the 'real' fields already stored on a cell and derive the
    two computed ones (pending_value, variance) from them.

    None propagates rather than raising: a cell that hasn't been
    materialized yet, or one where actual/accrued/paid haven't been written,
    is a normal, common state -- not an error. Whatever writes actual_value
    (a manual entry, or a future ledger bridge) is outside the engine; this
    only reads what's already on the row.
    """
    if cell is None:
        return {"actual_value": None, "accrued_value": None, "paid_value": None,
                "pending_value": None, "variance": None}
    actual, accrued, paid = cell.actual_value, cell.accrued_value, cell.paid_value
    pending = (accrued - paid) if accrued is not None and paid is not None else None
    variance = (actual - projected_value) if actual is not None and projected_value is not None else None
    return {"actual_value": actual, "accrued_value": accrued, "paid_value": paid,
            "pending_value": pending, "variance": variance}


def _evaluate_rule(
    rule: Dict,
    row_id: int,
    period_id: int,
    sorted_period_ids: List[int],  # all period ids in sort order
    computed: Dict[Tuple[int, int], Optional[Decimal]],  # (row_id, period_id) → value
    row_signs: Dict[int, int],  # row_id → +1 (positive) / -1 (negative)
    real_pending: Dict[Tuple[int, int], Optional[Decimal]],  # (row_id, period_id) → accrued - paid (or None)
) -> Tuple[Optional[Decimal], str, Optional[str]]:
    """Evaluate a single rule and return (value, source_label, error).

    'computed' holds already-resolved values for other (row, period) pairs.
    'real_pending' holds each cell's own accrued_value - paid_value (None
    when either is missing), precomputed up front from cell_map — this is
    what lets carry_forward read a row's own pending history across periods,
    the same way 'computed' already lets previous_period/rolling_average
    read across periods.
    'error' is None for every supported rule type; it is only set to
    "unsupported_rule:<type>" for a rule type this engine does not (yet)
    implement, so callers can tell that apart from a row with no rule at all.
    """
    rule_type = rule.get("type", "empty")

    if rule_type == "empty":
        return None, EffectiveSource.EMPTY, None

    if rule_type == "constant":
        raw = rule.get("value")
        if raw is None:
            return None, EffectiveSource.EMPTY, None
        return Decimal(str(raw)), EffectiveSource.RULE, None

    if rule_type == "previous_period":
        # No "period_sort_order == 0 means first period" shortcut here on
        # purpose: sort_order 0 is only the first period for a sheet with no
        # backfilled history. Once historical periods exist (negative
        # sort_order, see periods.extend_periods_backward), a period at
        # sort_order 0 legitimately has a previous period to read. Index
        # position in sorted_period_ids is the only thing that actually
        # tells us whether a previous period exists.
        prev_index = sorted_period_ids.index(period_id) - 1
        if prev_index < 0:
            return None, EffectiveSource.EMPTY, None
        prev_period_id = sorted_period_ids[prev_index]
        # Defaults to this row's own previous value; "row_id" lets a row
        # reference ANOTHER row's previous period (e.g. a running balance).
        source_row_id = rule.get("row_id", row_id)
        value = computed.get((source_row_id, prev_period_id))
        return value, EffectiveSource.RULE, None

    if rule_type == "sum_rows":
        dep_row_ids: List[int] = rule.get("row_ids", [])
        total = Decimal("0")
        has_any = False
        for dep_rid in dep_row_ids:
            val = computed.get((dep_rid, period_id))
            if val is not None:
                sign = row_signs.get(dep_rid, 1)
                total += val * sign
                has_any = True
        return (total if has_any else None), EffectiveSource.RULE, None

    if rule_type == "percent_of_row":
        source_row_id = rule.get("row_id")
        percent = rule.get("percent")
        if source_row_id is None or percent is None:
            return None, EffectiveSource.EMPTY, None
        base_value = computed.get((source_row_id, period_id))
        if base_value is None:
            return None, EffectiveSource.EMPTY, None
        value = base_value * Decimal(str(percent)) / Decimal("100")
        return value, EffectiveSource.RULE, None

    if rule_type == "rolling_average":
        n = rule.get("n")
        if not n or n <= 0:
            return None, EffectiveSource.EMPTY, None
        current_index = sorted_period_ids.index(period_id)
        values: List[Decimal] = []
        for offset in range(1, n + 1):
            prev_index = current_index - offset
            if prev_index < 0:
                break
            prev_period_id = sorted_period_ids[prev_index]
            v = computed.get((row_id, prev_period_id))
            if v is not None:
                values.append(v)
        if not values:
            return None, EffectiveSource.EMPTY, None
        return sum(values) / Decimal(len(values)), EffectiveSource.RULE, None

    if rule_type == "carry_forward":
        base_rule = rule.get("base_rule")
        if base_rule and base_rule.get("type") == "carry_forward":
            # No legitimate use case for nesting, and it would need extra
            # cycle bookkeeping this design doesn't cover — reject instead
            # of recursing.
            return None, EffectiveSource.EMPTY, "unsupported_rule:carry_forward(nested)"
        current_index = sorted_period_ids.index(period_id)
        if current_index == 0:
            carried = None
        else:
            prev_period_id = sorted_period_ids[current_index - 1]
            carried = real_pending.get((row_id, prev_period_id))
        carried_amount = carried if (carried is not None and carried > 0) else Decimal("0")
        base_value, base_source, base_error = _evaluate_rule(
            base_rule or {}, row_id, period_id, sorted_period_ids, computed, row_signs, real_pending
        )
        if base_error:
            return None, EffectiveSource.EMPTY, base_error
        if base_value is None and carried_amount == 0:
            return None, EffectiveSource.EMPTY, None
        base_amount = base_value if base_value is not None else Decimal("0")
        return base_amount + carried_amount, EffectiveSource.RULE, None

    # Unsupported rule type — surfaced as an error, not a silent empty cell.
    return None, EffectiveSource.EMPTY, f"unsupported_rule:{rule_type}"


def compute_sheet(sheet_id: int, db: Session) -> Dict:
    """Compute all cell values for a sheet and return the result dict.

    Returns a dict with structure:
    {
        "sheet": <CashflowSheet ORM object>,
        "periods": [<SheetPeriod>, ...],
        "sections": [
            {
                "section": <SheetSection>,
                "rows": [
                    {
                        "row": <SheetRow>,
                        "cells": [<CellResult>, ...]
                    }
                ]
            }
        ]
    }
    """
    sheet = db.query(CashflowSheet).filter(CashflowSheet.id == sheet_id).first()
    if sheet is None:
        raise ValueError(f"Sheet {sheet_id} not found")

    # Load all periods sorted
    periods: List[SheetPeriod] = (
        db.query(SheetPeriod)
        .filter(SheetPeriod.sheet_id == sheet_id)
        .order_by(SheetPeriod.sort_order)
        .all()
    )
    sorted_period_ids = [p.id for p in periods]

    # Load sections with rows (ordered)
    sections: List[SheetSection] = (
        db.query(SheetSection)
        .filter(SheetSection.sheet_id == sheet_id)
        .order_by(SheetSection.sort_order)
        .all()
    )

    # Collect all row ids across all sections
    all_rows: List[SheetRow] = []
    for section in sections:
        rows = (
            db.query(SheetRow)
            .filter(SheetRow.section_id == section.id)
            .order_by(SheetRow.sort_order)
            .all()
        )
        all_rows.extend(rows)

    row_map: Dict[int, SheetRow] = {r.id: r for r in all_rows}
    all_row_ids = list(row_map.keys())
    # row_id → +1 ("positive", the default) / -1 ("negative"). Used by sum_rows
    # so a row can subtract from its parent while its own stored value stays
    # a positive magnitude.
    row_signs: Dict[int, int] = {
        r.id: (-1 if r.sign == "negative" else 1) for r in all_rows
    }

    # Load all existing cells for this sheet (to fetch persisted overrides)
    all_cells: List[SheetCell] = (
        db.query(SheetCell)
        .filter(SheetCell.row_id.in_(all_row_ids))
        .all()
    )
    # Index: (row_id, period_id) → SheetCell
    cell_map: Dict[Tuple[int, int], SheetCell] = {
        (c.row_id, c.period_id): c for c in all_cells
    }

    # Precompute each cell's own pending_value (accrued_value - paid_value,
    # only when BOTH are present — mirrors _real_fields exactly) up front so
    # a RULE (carry_forward) can read a row's own pending history across
    # periods, the same way 'computed' already lets previous_period/
    # rolling_average read across periods.
    real_pending: Dict[Tuple[int, int], Optional[Decimal]] = {}
    for (row_id, period_id), cell in cell_map.items():
        if cell.accrued_value is not None and cell.paid_value is not None:
            real_pending[(row_id, period_id)] = cell.accrued_value - cell.paid_value
        else:
            real_pending[(row_id, period_id)] = None

    # For each row determine its effective rule per period
    # effective_rules[(row_id, period_id)] = (rule_dict or None, is_manual_value, manual_value)
    effective_manual: Dict[Tuple[int, int], Decimal] = {}  # manual_value overrides
    effective_rules: Dict[Tuple[int, int], Optional[Dict]] = {}  # rule for each cell

    for row in all_rows:
        for period in periods:
            key = (row.id, period.id)
            cell = cell_map.get(key)
            if cell is not None:
                active_ov = _get_active_override(cell.overrides)
                if active_ov is not None:
                    if active_ov.override_type == "manual_value":
                        effective_manual[key] = active_ov.value
                        effective_rules[key] = None  # won't use rule
                        continue
                    if active_ov.override_type == "manual_rule":
                        effective_rules[key] = active_ov.custom_rule
                        continue
                    if active_ov.override_type == "lock":
                        # Frozen at whatever value was captured when the lock
                        # was created (see the consuming app's override
                        # write path) — from here on it behaves exactly like
                        # a manual_value override. The engine doesn't capture
                        # anything itself; it only respects what's stored.
                        effective_manual[key] = active_ov.value
                        effective_rules[key] = None
                        continue
            # Fall back to row default rule
            effective_rules[key] = row.default_projection_rule

    # Build same-period dependency graph for cycle detection
    # adjacency: row_id → [row_ids it depends on in the same period]
    adjacency: Dict[int, List[int]] = {rid: [] for rid in all_row_ids}
    for row in all_rows:
        # Use any representative period to extract deps (rule is same for all periods at row level)
        rule = row.default_projection_rule
        if rule:
            deps = _extract_same_period_deps(rule)
            adjacency[row.id] = [d for d in deps if d in row_map]
    # Also consider manual_rule overrides that override the row-level rule
    # For simplicity, also scan per-cell rules; the union is what matters for cycle detection
    for (row_id, period_id), rule in effective_rules.items():
        if rule and (row_id, period_id) not in effective_manual:
            deps = _extract_same_period_deps(rule)
            existing = adjacency.get(row_id, [])
            for d in deps:
                if d in row_map and d not in existing:
                    existing.append(d)
            adjacency[row_id] = existing

    cyclic_rows: Set[int] = _detect_cycles(all_row_ids, adjacency)

    # Evaluate cells in topological order per period
    # Strategy: process periods in sort_order; within each period, do topo sort of rows
    computed: Dict[Tuple[int, int], Optional[Decimal]] = {}

    # Topological order of rows (ignoring cycles — those get None + error)
    def topo_sort_rows(rows: List[int], adj: Dict[int, List[int]]) -> List[int]:
        visited: Set[int] = set()
        order: List[int] = []

        def visit(node: int, depth: int = 0) -> None:
            if node in visited or depth > MAX_DEPENDENCY_DEPTH:
                return
            visited.add(node)
            for neighbor in adj.get(node, []):
                visit(neighbor, depth + 1)
            order.append(node)

        for rid in rows:
            visit(rid)
        return order

    non_cyclic_rows = [r for r in all_row_ids if r not in cyclic_rows]
    topo_order = topo_sort_rows(non_cyclic_rows, adjacency)

    results_by_row_period: Dict[Tuple[int, int], CellResult] = {}

    for period in periods:
        # First process non-cyclic rows in topological order
        for row_id in topo_order:
            key = (row_id, period.id)
            if key in effective_manual:
                val = effective_manual[key]
                computed[key] = val
                results_by_row_period[key] = CellResult(
                    row_id=row_id,
                    period_id=period.id,
                    projected_value=val,
                    effective_source=EffectiveSource.MANUAL,
                    **_real_fields(cell_map.get(key), val),
                )
            else:
                rule = effective_rules.get(key)
                if rule is None:
                    computed[key] = None
                    results_by_row_period[key] = CellResult(
                        row_id=row_id,
                        period_id=period.id,
                        projected_value=None,
                        effective_source=EffectiveSource.EMPTY,
                        **_real_fields(cell_map.get(key), None),
                    )
                else:
                    val, source, rule_error = _evaluate_rule(
                        rule=rule,
                        row_id=row_id,
                        period_id=period.id,
                        sorted_period_ids=sorted_period_ids,
                        computed=computed,
                        row_signs=row_signs,
                        real_pending=real_pending,
                    )
                    computed[key] = val
                    results_by_row_period[key] = CellResult(
                        row_id=row_id,
                        period_id=period.id,
                        projected_value=val,
                        effective_source=source,
                        error=rule_error,
                        **_real_fields(cell_map.get(key), val),
                    )

        # Then mark cyclic rows. The cycle only invalidates the PROJECTION --
        # any real data already recorded on the cell is still legitimate and
        # still surfaces (variance is None regardless, since there's no
        # projected_value to compare against).
        for row_id in cyclic_rows:
            key = (row_id, period.id)
            computed[key] = None
            results_by_row_period[key] = CellResult(
                row_id=row_id,
                period_id=period.id,
                projected_value=None,
                effective_source=EffectiveSource.EMPTY,
                error="cycle_detected",
                **_real_fields(cell_map.get(key), None),
            )

    # Assemble final structure
    result_sections = []
    for section in sections:
        section_rows = (
            db.query(SheetRow)
            .filter(SheetRow.section_id == section.id)
            .order_by(SheetRow.sort_order)
            .all()
        )
        row_outputs = []
        for row in section_rows:
            cells = [
                results_by_row_period.get(
                    (row.id, period.id),
                    CellResult(
                        row_id=row.id,
                        period_id=period.id,
                        projected_value=None,
                        effective_source=EffectiveSource.EMPTY,
                    ),
                )
                for period in periods
            ]
            row_outputs.append({"row": row, "cells": cells})
        result_sections.append({"section": section, "rows": row_outputs})

    return {
        "sheet": sheet,
        "periods": periods,
        "sections": result_sections,
    }
