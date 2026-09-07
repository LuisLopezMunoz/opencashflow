"""Tests for opencashflow.creditcard_statements.

Direct ORM construction on a single throwaway sqlite engine/registry (same
style as test_period_close.py/test_engine_rules.py) -- opencashflow.credit_card's
CreditCard/CreditCardCharge/CreditCardStatement/CreditCardStatementLine share
opencashflow.models.Base, so this package never needs a second registry or
a real user account; `user_id`/`created_by` are just plain ints (TEST_USER_ID).

Covers:
  1. installment_schedule with an amount that doesn't divide evenly --
     exact sum equality, remainder on the last cuota.
  2. compute_instant_statement with no prior real statement
     (previous_balance_known=False) and with one (uses its new_balance).
  3. compute_instant_statement never double-counts an installment that
     already has a REAL CreditCardStatementLine recorded elsewhere.
  4. sync_period branch A (real statement -> accrued_value write +
     CellActualEntry) and branch B (no real statement -> CellOverride
     write), both with exact numbers computed by hand.
  5. sync_period falls back to the card's own mapped_sheet_id/mapped_row_id
     when the caller passes sheet=None/row=None.
  6. sync_period with no card mapping and no explicit sheet/row raises
     ValueError.
  7. dry_run=True persists nothing.
  8. resolve_credit_card -- exact/substring/ambiguous/not-found.
  9. payment lag (due_day < closing_day) shifts which billing_period a
     sheet payment-month period resolves to.
  10. cupo_disponible / _cupo_disponible_real_ahora -- real vs. estimate,
      and exclude_category excluding a tagged charge from today's real cupo.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.credit_card import CreditCard, CreditCardCharge, CreditCardStatement, CreditCardStatementLine
from opencashflow.creditcard_statements import (
    InstantStatement,
    SyncReport,
    _cupo_disponible_real_ahora,
    compute_instant_statement,
    cupo_disponible,
    installment_schedule,
    resolve_credit_card,
    sync_period,
)
from opencashflow.models import Base, CashflowSheet, CellActualEntry, CellOverride, SheetCell, SheetPeriod, SheetRow, SheetSection

TEST_USER_ID = 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def sheet(db):
    # 3 months starting 2026-01 -> periods for 2026-01, 2026-02, 2026-03.
    s = CashflowSheet(user_id=TEST_USER_ID, name="Tarjeta Test Sheet", currency="CLP",
                       horizon_months=3, base_period=date(2026, 1, 1))
    db.add(s)
    db.flush()
    for i in range(3):
        db.add(SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 1 + i, 1), label=f"P{i}", sort_order=i))
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def row(db, sheet):
    section = SheetSection(sheet_id=sheet.id, name="Tarjetas", section_type="expense")
    db.add(section)
    db.flush()
    r = SheetRow(section_id=section.id, name="Tarjeta Principal - Total a Pagar", row_type="input", sign="negative")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@pytest.fixture()
def card(db):
    # due_day >= closing_day -> zero payment lag (see _payment_lag_months),
    # so every OTHER test in this file (written before the lag concept
    # existed) can keep treating `period` and `billing_period` as the same
    # month without being rewritten. test_payment_lag_* below use their own
    # real-world-shaped fixture instead, where due_day < closing_day.
    c = CreditCard(
        user_id=TEST_USER_ID, name="Tarjeta Principal", bank="Banco X", credit_limit=5_000_000.0,
        current_balance=0.0, currency="CLP", closing_day=25, due_day=28,
        interest_rate=0.2, minimum_payment_rate=0.05,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture()
def card_with_lag(db):
    # Real-world shape (matches actual Santander/BCI statements): closing_day=25,
    # due_day=9 -- due_day < closing_day, so the bill closes in month M and
    # is paid in month M+1.
    c = CreditCard(
        user_id=TEST_USER_ID, name="Tarjeta Con Desfase", bank="Banco Y", credit_limit=3_000_000.0,
        current_balance=0.0, currency="CLP", closing_day=25, due_day=9,
        interest_rate=0.0345, minimum_payment_rate=0.05,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _period(db, sheet, year: int, month: int) -> SheetPeriod:
    p = db.query(SheetPeriod).filter(
        SheetPeriod.sheet_id == sheet.id, SheetPeriod.period_date == datetime(year, month, 1),
    ).first()
    assert p is not None, f"no se generó el período {year}-{month:02d} para la planilla de prueba"
    return p


def _cell(db, row_id: int, period_id: int):
    return db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()


def _active_override(db, cell_id: int):
    return db.query(CellOverride).filter(CellOverride.cell_id == cell_id, CellOverride.superseded_at.is_(None)).first()


def _add_statement(db, card, billing_period: date, *, new_balance, total_payment_due, previous_balance=None,
                    remaining_principal_installments=None, created_by=TEST_USER_ID,
                    closing_date=None, due_date=None) -> CreditCardStatement:
    stmt = CreditCardStatement(
        credit_card_id=card.id, billing_period=billing_period,
        previous_balance=Decimal(str(previous_balance)) if previous_balance is not None else None,
        new_balance=Decimal(str(new_balance)), total_payment_due=Decimal(str(total_payment_due)),
        remaining_principal_installments=(
            Decimal(str(remaining_principal_installments)) if remaining_principal_installments is not None else None
        ),
        closing_date=closing_date or billing_period, due_date=due_date or billing_period,
        currency="CLP", source="manual", created_by=created_by,
    )
    db.add(stmt)
    db.commit()
    db.refresh(stmt)
    return stmt


def _add_charge(db, card, *, amount, installments, first_statement_period, description="Compra", category="General"):
    charge = CreditCardCharge(
        credit_card_id=card.id, amount=amount, description=description, category=category,
        charge_date=first_statement_period, installments=installments, installments_paid=0,
        first_statement_period=first_statement_period,
    )
    db.add(charge)
    db.commit()
    db.refresh(charge)
    return charge


# ---------------------------------------------------------------------------
# 1. installment_schedule
# ---------------------------------------------------------------------------

def test_installment_schedule_uneven_amount_sums_exactly_remainder_on_last(db):
    schedule = installment_schedule(Decimal("1000000"), 3)
    assert schedule == [Decimal("333333"), Decimal("333333"), Decimal("333334")]
    assert sum(schedule) == Decimal("1000000")


# ---------------------------------------------------------------------------
# 2. compute_instant_statement: previous_balance known / unknown
# ---------------------------------------------------------------------------

def test_compute_instant_statement_no_prior_statement_previous_balance_unknown(db, card):
    result = compute_instant_statement(db, card, date(2026, 3, 1))
    assert isinstance(result, InstantStatement)
    assert result.is_real is False
    assert result.previous_balance_known is False
    assert result.previous_balance is None
    assert result.lines == []
    assert result.charges_due_this_cycle == Decimal("0")


def test_compute_instant_statement_uses_prior_statement_new_balance(db, card):
    _add_statement(db, card, date(2026, 1, 1), new_balance=250_000, total_payment_due=50_000)
    result = compute_instant_statement(db, card, date(2026, 3, 1))
    assert result.is_real is False  # no statement recorded for 2026-03 itself
    assert result.previous_balance_known is True
    assert result.previous_balance == Decimal("250000")


def test_compute_instant_statement_wraps_real_statement_without_projection(db, card):
    stmt = _add_statement(
        db, card, date(2026, 3, 1), new_balance=400_000, total_payment_due=120_000, previous_balance=280_000,
    )
    db.add(CreditCardStatementLine(
        statement_id=stmt.id, line_type="charge", description="Restaurante", amount=Decimal("30000"),
        transaction_date=date(2026, 2, 15),
    ))
    db.commit()

    result = compute_instant_statement(db, card, date(2026, 3, 1))
    assert result.is_real is True
    assert result.previous_balance == Decimal("280000")
    assert result.previous_balance_known is True
    assert result.charges_due_this_cycle == Decimal("120000")
    assert len(result.lines) == 1
    assert result.lines[0].description == "Restaurante"
    assert result.lines[0].is_projected is False


# ---------------------------------------------------------------------------
# 3. never double-counts an installment already recorded as a real line
# ---------------------------------------------------------------------------

def test_compute_instant_statement_skips_installment_already_real_elsewhere(db, card):
    # 6-cuota purchase starting Jan 2026: cuota 3 lands in March 2026.
    charge = _add_charge(db, card, amount=600_000, installments=6, first_statement_period=date(2026, 1, 1))

    # Cuota 3 was already recorded as a REAL line on a DIFFERENT (already
    # real) statement -- e.g. a correction entered directly against May's
    # statement. This must never be double counted in March's projection.
    other_stmt = _add_statement(db, card, date(2026, 5, 1), new_balance=100_000, total_payment_due=100_000)
    db.add(CreditCardStatementLine(
        statement_id=other_stmt.id, charge_id=charge.id, line_type="charge",
        installment_number=3, installment_total=6, description="Compra (cuota 3/6, corrección)",
        transaction_date=date(2026, 1, 1), amount=Decimal("100000"),
    ))
    db.commit()

    result = compute_instant_statement(db, card, date(2026, 3, 1))
    assert result.is_real is False
    assert result.lines == []
    assert result.charges_due_this_cycle == Decimal("0")

    # Sanity check: a period whose installment is NOT already real (cuota 4,
    # April) still projects normally -- proves the skip is specific to the
    # (charge_id, installment_number) pair, not the whole charge.
    result_april = compute_instant_statement(db, card, date(2026, 4, 1))
    assert len(result_april.lines) == 1
    assert result_april.lines[0].installment_number == 4
    assert result_april.lines[0].is_projected is True
    assert result_april.charges_due_this_cycle == Decimal("100000")  # 600000 // 6


# ---------------------------------------------------------------------------
# 4. sync_period branch A / branch B -- exact numbers computed by hand
# ---------------------------------------------------------------------------

def test_sync_period_branch_a_real_statement_writes_accrued_value(db, card, sheet, row):
    period_row = _period(db, sheet, 2026, 2)
    _add_statement(db, card, date(2026, 2, 1), new_balance=180_000, total_payment_due=145_670, previous_balance=200_000)

    report = sync_period(db, card, sheet, row, date(2026, 2, 1), TEST_USER_ID)

    assert isinstance(report, SyncReport)
    assert report.branch == "real_statement"
    assert report.old_value is None
    assert report.new_value == Decimal("145670")
    assert report.dry_run is False

    cell = _cell(db, row.id, period_row.id)
    assert cell is not None
    assert cell.accrued_value == Decimal("145670")

    entries = db.query(CellActualEntry).filter(CellActualEntry.cell_id == cell.id).all()
    assert len(entries) == 1
    assert entries[0].accrued_value == Decimal("145670")
    assert entries[0].created_by == TEST_USER_ID
    # No CellOverride is ever written by branch A.
    assert _active_override(db, cell.id) is None


def test_sync_period_branch_b_projection_writes_override(db, card, sheet, row):
    period_row = _period(db, sheet, 2026, 1)
    # Charge #1: 1,000,000 / 3 cuotas, cuota 1 lands exactly in 2026-01 -> 333,333.
    _add_charge(db, card, amount=1_000_000, installments=3, first_statement_period=date(2026, 1, 1),
                description="Notebook")
    # Charge #2: single payment, also due this cycle -> 50,000.
    _add_charge(db, card, amount=50_000, installments=1, first_statement_period=date(2026, 1, 1),
                description="Supermercado")
    # Charge #3: doesn't start until March -- must NOT be included.
    _add_charge(db, card, amount=999_999, installments=1, first_statement_period=date(2026, 3, 1),
                description="Futuro")

    report = sync_period(db, card, sheet, row, date(2026, 1, 1), TEST_USER_ID)

    assert report.branch == "projection"
    assert report.old_value is None
    assert report.new_value == Decimal("333333") + Decimal("50000")  # 383,333
    assert report.dry_run is False

    cell = _cell(db, row.id, period_row.id)
    assert cell is not None
    override = _active_override(db, cell.id)
    assert override is not None
    assert override.override_type == "manual_value"
    assert override.value == Decimal("383333")
    # Branch B never touches accrued_value -- that's real-layer data, and
    # nothing real has been recorded for this cell yet.
    assert cell.accrued_value is None


def test_sync_period_branch_b_supersedes_previous_override(db, card, sheet, row):
    period_row = _period(db, sheet, 2026, 1)
    _add_charge(db, card, amount=300_000, installments=1, first_statement_period=date(2026, 1, 1))

    first = sync_period(db, card, sheet, row, date(2026, 1, 1), TEST_USER_ID)
    assert first.new_value == Decimal("300000")

    # A second charge appears before the real statement is ever recorded --
    # syncing again must supersede the first override, not stack a second
    # active one.
    _add_charge(db, card, amount=20_000, installments=1, first_statement_period=date(2026, 1, 1))
    second = sync_period(db, card, sheet, row, date(2026, 1, 1), TEST_USER_ID)

    assert second.old_value == Decimal("300000")
    assert second.new_value == Decimal("320000")

    cell = _cell(db, row.id, period_row.id)
    all_overrides = db.query(CellOverride).filter(CellOverride.cell_id == cell.id).all()
    assert len(all_overrides) == 2
    active = [o for o in all_overrides if o.superseded_at is None]
    assert len(active) == 1
    assert active[0].value == Decimal("320000")


# ---------------------------------------------------------------------------
# 5. falls back to the card's own mapping when sheet/row aren't passed
# ---------------------------------------------------------------------------

def test_sync_period_uses_card_mapping_when_sheet_and_row_are_none(db, card, sheet, row):
    card.mapped_sheet_id = sheet.id
    card.mapped_row_id = row.id
    db.commit()

    _add_charge(db, card, amount=90_000, installments=1, first_statement_period=date(2026, 1, 1))

    report = sync_period(db, card, None, None, date(2026, 1, 1), TEST_USER_ID)

    assert report.sheet_id == sheet.id
    assert report.row_id == row.id
    assert report.new_value == Decimal("90000")

    period_row = _period(db, sheet, 2026, 1)
    cell = _cell(db, row.id, period_row.id)
    assert _active_override(db, cell.id).value == Decimal("90000")


# ---------------------------------------------------------------------------
# 6. no mapping and no explicit sheet/row -> ValueError
# ---------------------------------------------------------------------------

def test_sync_period_raises_without_mapping_or_explicit_target(db, card):
    assert card.mapped_sheet_id is None
    assert card.mapped_row_id is None

    with pytest.raises(ValueError, match="creditcard map"):
        sync_period(db, card, None, None, date(2026, 1, 1), TEST_USER_ID)


# ---------------------------------------------------------------------------
# 7. dry_run persists nothing
# ---------------------------------------------------------------------------

def test_sync_period_dry_run_persists_nothing(db, card, sheet, row):
    period_row = _period(db, sheet, 2026, 1)
    _add_charge(db, card, amount=60_000, installments=1, first_statement_period=date(2026, 1, 1))

    before_cell = _cell(db, row.id, period_row.id)
    assert before_cell is None
    before_override_count = db.query(CellOverride).count()

    report = sync_period(db, card, sheet, row, date(2026, 1, 1), TEST_USER_ID, dry_run=True)

    assert report.dry_run is True
    assert report.new_value == Decimal("60000")

    assert db.query(CellOverride).count() == before_override_count
    assert _cell(db, row.id, period_row.id) is None

    # A real (non-dry-run) sync afterward behaves identically and DOES persist.
    real_report = sync_period(db, card, sheet, row, date(2026, 1, 1), TEST_USER_ID, dry_run=False)
    assert real_report.new_value == Decimal("60000")
    cell = _cell(db, row.id, period_row.id)
    assert cell is not None
    assert _active_override(db, cell.id).value == Decimal("60000")


def test_sync_period_dry_run_branch_a_leaves_accrued_value_untouched(db, card, sheet, row):
    period_row = _period(db, sheet, 2026, 2)
    _add_statement(db, card, date(2026, 2, 1), new_balance=10_000, total_payment_due=99_999)

    report = sync_period(db, card, sheet, row, date(2026, 2, 1), TEST_USER_ID, dry_run=True)
    assert report.branch == "real_statement"
    assert report.new_value == Decimal("99999")

    cell = _cell(db, row.id, period_row.id)
    assert cell is None or cell.accrued_value is None
    assert db.query(CellActualEntry).count() == 0


# ---------------------------------------------------------------------------
# 8. resolve_credit_card -- exact / substring / ambiguous / not found
# ---------------------------------------------------------------------------

def test_resolve_credit_card_by_id(db, card):
    assert resolve_credit_card(db, None, str(card.id)).id == card.id


def test_resolve_credit_card_by_exact_name(db, card):
    assert resolve_credit_card(db, None, "Tarjeta Principal").id == card.id


def test_resolve_credit_card_by_substring(db, card):
    assert resolve_credit_card(db, None, "principal").id == card.id


def test_resolve_credit_card_not_found_raises(db, card):
    with pytest.raises(ValueError, match="No se encontró"):
        resolve_credit_card(db, None, "no existe")


def test_resolve_credit_card_ambiguous_raises_naming_candidates(db):
    db.add_all([
        CreditCard(user_id=TEST_USER_ID, name="Tarjeta Visa", credit_limit=1_000_000.0, current_balance=0.0),
        CreditCard(user_id=TEST_USER_ID, name="Tarjeta Mastercard", credit_limit=1_000_000.0, current_balance=0.0),
    ])
    db.commit()
    with pytest.raises(ValueError, match="ambiguo"):
        resolve_credit_card(db, None, "Tarjeta")


# ---------------------------------------------------------------------------
# 9. payment lag (due_day < closing_day -> a statement that closes in month M
#    is paid in month M+1) -- real-world behavior confirmed against two real
#    bank statements (Santander closing_day=25/due_day=9, BCI closing_day=25/
#    due_day=5), both card_with_lag fixture's shape.
# ---------------------------------------------------------------------------

def test_compute_instant_statement_finds_real_statement_one_month_after_closing(db, card_with_lag):
    # Statement closes 2026-08 -> its total is due/paid in 2026-09, so asking
    # for the SHEET period 2026-09 must find this statement (billing_period
    # 2026-08), not look for one dated 2026-09 (which doesn't exist).
    stmt = _add_statement(
        db, card_with_lag, date(2026, 8, 1), new_balance=800_000, total_payment_due=786_213,
        previous_balance=2_366_294,
    )
    db.add(CreditCardStatementLine(
        statement_id=stmt.id, line_type="charge", description="Compra", amount=Decimal("10000"),
        transaction_date=date(2026, 8, 1),
    ))
    db.commit()

    result_payment_month = compute_instant_statement(db, card_with_lag, date(2026, 9, 1))
    assert result_payment_month.is_real is True
    assert result_payment_month.charges_due_this_cycle == Decimal("786213")
    # The InstantStatement's own `period` still reports the PAYMENT month
    # (what was asked for), not the billing month.
    assert result_payment_month.period == date(2026, 9, 1)

    # Asking for the billing month itself (2026-08) must NOT find this
    # statement -- it isn't due/paid until September, per this card's lag.
    result_billing_month = compute_instant_statement(db, card_with_lag, date(2026, 8, 1))
    assert result_billing_month.is_real is False


def test_compute_instant_statement_projection_uses_lag_shifted_billing_period(db, card_with_lag):
    # A charge whose cuota #1 was billed in 2026-08 (first_statement_period)
    # is due/paid in 2026-09 -- projecting for sheet period 2026-09 must
    # therefore compute installment_number=1, not treat 2026-09 as the
    # billing month directly (which would incorrectly compute cuota #2).
    _add_charge(db, card_with_lag, amount=300_000, installments=3, first_statement_period=date(2026, 8, 1))

    result = compute_instant_statement(db, card_with_lag, date(2026, 9, 1))
    assert result.is_real is False
    assert len(result.lines) == 1
    assert result.lines[0].installment_number == 1
    assert result.charges_due_this_cycle == Decimal("100000")  # 300000 // 3

    # The FOLLOWING sheet period (October) should show cuota #2.
    result_next = compute_instant_statement(db, card_with_lag, date(2026, 10, 1))
    assert result_next.lines[0].installment_number == 2


def test_sync_period_branch_a_uses_lag_shifted_billing_period(db, card_with_lag, sheet, row):
    # sheet fixture only generates periods 2026-01..2026-03 -- reuse the
    # January period as the "payment month" and put the real statement's
    # billing_period one month earlier (December 2025) so the lag lookup is
    # exercised without needing a differently-shaped sheet fixture.
    period_row = _period(db, sheet, 2026, 1)
    _add_statement(
        db, card_with_lag, date(2025, 12, 1), new_balance=500_000, total_payment_due=350_797,
        previous_balance=340_690,
    )

    report = sync_period(db, card_with_lag, sheet, row, date(2026, 1, 1), TEST_USER_ID)

    assert report.branch == "real_statement"
    assert report.new_value == Decimal("350797")
    cell = _cell(db, row.id, period_row.id)
    assert cell.accrued_value == Decimal("350797")


# ---------------------------------------------------------------------------
# 10. cupo_disponible / _cupo_disponible_real_ahora
# ---------------------------------------------------------------------------

def test_cupo_disponible_estimate_from_active_installments_before_closing(db, card):
    # closing_day=25 -- today (2026-01-10) is BEFORE closing, so the open
    # cycle is 2026-01 itself; a charge whose #1 cuota already landed in
    # 2026-01 has all 3 of its cuotas still committed (nothing "closed" yet).
    _add_charge(db, card, amount=900_000, installments=3, first_statement_period=date(2026, 1, 1))
    cupo = cupo_disponible(db, card, date(2026, 1, 10))
    assert cupo == Decimal("5000000") - Decimal("900000")


def test_cupo_disponible_uses_real_remaining_principal_when_reported(db, card):
    # Statement for the cycle closing 2025-12 reports the bank's own real
    # SALDO CAPITAL CUOTAS -- once that exists, cupo_disponible must use it
    # instead of an installment-based estimate for the NOW-open 2026-01 cycle.
    _add_statement(
        db, card, date(2025, 12, 1), new_balance=1_000_000, total_payment_due=200_000,
        remaining_principal_installments=1_500_000,
    )
    cupo = cupo_disponible(db, card, date(2026, 1, 10))
    assert cupo == Decimal("5000000") - Decimal("1500000")


def test_cupo_disponible_real_ahora_subtracts_unpaid_last_bill(db, card, sheet, row):
    card.mapped_sheet_id = sheet.id
    card.mapped_row_id = row.id
    db.commit()

    # Previous cycle (2025-12) closed with a real bill that hasn't been paid
    # yet (no paid_value ever recorded on the mapped cell for its payment
    # period) -- _cupo_disponible_real_ahora must subtract it in full, unlike
    # the planning-oriented cupo_disponible which assumes it's already paid.
    _add_statement(
        db, card, date(2025, 12, 1), new_balance=1_000_000, total_payment_due=200_000,
        remaining_principal_installments=1_500_000,
    )

    real_cupo, is_estimate = _cupo_disponible_real_ahora(db, card, date(2026, 1, 10))
    assert is_estimate is False
    assert real_cupo == Decimal("5000000") - Decimal("1500000") - Decimal("200000")


def test_cupo_disponible_real_ahora_excludes_tagged_category(db, card):
    # A charge tagged with a caller-chosen "planned draw" category represents
    # a plan the bank hasn't seen yet -- exclude_category must keep it from
    # occupying room in today's real cupo, while an ordinary charge (no
    # matching category) still counts against it.
    _add_charge(db, card, amount=300_000, installments=1, first_statement_period=date(2026, 1, 1),
                category="Financiamiento puente")
    _add_charge(db, card, amount=100_000, installments=1, first_statement_period=date(2026, 1, 1),
                category="General")

    real_cupo_excluded, _ = _cupo_disponible_real_ahora(
        db, card, date(2026, 1, 10), exclude_category="Financiamiento puente",
    )
    real_cupo_included, _ = _cupo_disponible_real_ahora(db, card, date(2026, 1, 10))

    assert real_cupo_excluded == Decimal("5000000") - Decimal("100000")
    assert real_cupo_included == Decimal("5000000") - Decimal("400000")
