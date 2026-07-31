"""Cashflow sheet calculation engine.

Computes projected values for all cells of a sheet using the following
priority order per cell:

  1. Active CellOverride with override_type = 'manual_value'  → use override.value
  2. Active CellOverride with override_type = 'manual_rule'   → evaluate custom_rule
  3. SheetRow.default_projection_rule                         → evaluate row rule
  4. No rule                                                  → None (empty)

Supported rules in V1:
  - constant          { "type": "constant", "value": <number> }
  - previous_period   { "type": "previous_period" }
  - sum_rows          { "type": "sum_rows", "row_ids": [<int>, ...] }

Rules deferred to later iterations:
  - rolling_average, running_balance, percent_of_row, ledger_aggregate

Cycle detection:
  Before evaluating, the engine builds the intra-period dependency graph
  (only same-period rules can create cycles; previous_period never does).
  It runs a DFS topological sort and marks any cell participating in a
  cycle with error='cycle_detected'.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

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
    """Return list of row_ids that this rule depends on in the SAME period."""
    if not rule:
        return []
    rule_type = rule.get("type")
    if rule_type == "sum_rows":
        return list(rule.get("row_ids", []))
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


def _evaluate_rule(
    rule: Dict,
    row_id: int,
    period_id: int,
    period_sort_order: int,
    sorted_period_ids: List[int],  # all period ids in sort order
    computed: Dict[Tuple[int, int], Optional[Decimal]],  # (row_id, period_id) → value
) -> Tuple[Optional[Decimal], str]:
    """Evaluate a single rule and return (value, source_label).

    'computed' holds already-resolved values for other (row, period) pairs.
    """
    rule_type = rule.get("type", "empty")

    if rule_type == "empty":
        return None, EffectiveSource.EMPTY

    if rule_type == "constant":
        raw = rule.get("value")
        if raw is None:
            return None, EffectiveSource.EMPTY
        return Decimal(str(raw)), EffectiveSource.RULE

    if rule_type == "previous_period":
        if period_sort_order == 0:
            # First period — no previous exists
            return None, EffectiveSource.EMPTY
        prev_index = sorted_period_ids.index(period_id) - 1
        if prev_index < 0:
            return None, EffectiveSource.EMPTY
        prev_period_id = sorted_period_ids[prev_index]
        value = computed.get((row_id, prev_period_id))
        return value, EffectiveSource.RULE

    if rule_type == "sum_rows":
        dep_row_ids: List[int] = rule.get("row_ids", [])
        total = Decimal("0")
        has_any = False
        for dep_rid in dep_row_ids:
            val = computed.get((dep_rid, period_id))
            if val is not None:
                total += val
                has_any = True
        return (total if has_any else None), EffectiveSource.RULE

    # Unsupported rule type in V1
    return None, EffectiveSource.EMPTY


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
    from backend.cashflow.models import CashflowSheet, SheetCell, SheetPeriod, SheetRow, SheetSection

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
    period_sort_orders = {p.id: p.sort_order for p in periods}

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
                    )
                else:
                    val, source = _evaluate_rule(
                        rule=rule,
                        row_id=row_id,
                        period_id=period.id,
                        period_sort_order=period_sort_orders[period.id],
                        sorted_period_ids=sorted_period_ids,
                        computed=computed,
                    )
                    computed[key] = val
                    results_by_row_period[key] = CellResult(
                        row_id=row_id,
                        period_id=period.id,
                        projected_value=val,
                        effective_source=source,
                    )

        # Then mark cyclic rows
        for row_id in cyclic_rows:
            key = (row_id, period.id)
            computed[key] = None
            results_by_row_period[key] = CellResult(
                row_id=row_id,
                period_id=period.id,
                projected_value=None,
                effective_source=EffectiveSource.EMPTY,
                error="cycle_detected",
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
