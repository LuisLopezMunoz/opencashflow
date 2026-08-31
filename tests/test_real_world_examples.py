"""Structural stress-tests against real-world spreadsheet examples.

Unlike test_engine_rules.py (one rule/behavior per test), these recreate the
full row/section layout of publicly available cash-flow and budget
templates, to confirm the existing rule catalog (constant, sum_rows,
previous_period, and, since the actual/accrued/paid passthrough, the
projected-vs-real comparison) covers shapes other than the personal
household example that ships in opencashflow.seed -- without needing any
new rule type.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CashflowSheet, CellOverride, SheetCell, SheetPeriod, SheetRow, SheetSection
from opencashflow.periods import generate_periods

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


def _cells_for(matrix, row_id):
    for section in matrix["sections"]:
        for row_data in section["rows"]:
            if row_data["row"].id == row_id:
                return row_data["cells"]
    raise AssertionError(f"row {row_id} not found in matrix")


def _add_row(db, section, name, sort_order, sign="positive", row_type="input"):
    row = SheetRow(section_id=section.id, name=name, sort_order=sort_order, sign=sign, row_type=row_type)
    db.add(row)
    return row


def _override(db, row_id, period_id, value):
    cell = SheetCell(row_id=row_id, period_id=period_id)
    db.add(cell)
    db.flush()
    db.add(CellOverride(cell_id=cell.id, value=Decimal(str(value)), override_type="manual_value",
                         created_by=TEST_USER_ID))


def test_operating_investing_financing_statement_with_running_balance(db):
    """Recreates a classic 3-way cash flow statement (Operativo / Inversion /
    Financiero, each netting its own Cobros/Pagos lines, feeding a single
    running cash balance) adapted from a public template
    (todoexcel.com / excelnegocios.com, "Flujo de Caja Mensual").

    Numbers are the template's own month-1 example, reproduced with the
    CORRECT accounting sign convention (pagos = negative). The template
    itself entered every line as a positive number, so its own "Suma de
    pagos" formula (a SUMIF filtering by cell sign) silently computes 0, and
    its "Flujos operativos" subtotal (31200) is gross combined activity, not
    a net figure. opencashflow is not exposed to that class of bug: sign is
    a property of the ROW (SheetRow.sign), declared once, never inferred
    from whatever number a cell happens to hold -- so this test asserts the
    CORRECT net result (5800), not the template's own miscalculated one.
    """
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Flujo de Caja Mensual (todoexcel.com)",
                           currency="USD", horizon_months=12, base_period=datetime(2026, 1, 1))
    db.add(sheet)
    db.flush()
    generate_periods(sheet, db)
    db.flush()

    sec_op = SheetSection(sheet_id=sheet.id, name="Flujos operativos", sort_order=0)
    sec_inv = SheetSection(sheet_id=sheet.id, name="Flujos de inversion", sort_order=1)
    sec_fin = SheetSection(sheet_id=sheet.id, name="Flujos financieros", sort_order=2)
    sec_res = SheetSection(sheet_id=sheet.id, name="Resumen", section_type="balance", sort_order=3)
    db.add_all([sec_op, sec_inv, sec_fin, sec_res])
    db.flush()

    r_cobro_contado = _add_row(db, sec_op, "Cobros por ventas al contado", 0)
    r_cobro_plazo = _add_row(db, sec_op, "Cobros por ventas a plazo", 1)
    r_pago_nomina = _add_row(db, sec_op, "Pagos de nominas", 2, sign="negative")
    r_pago_seg_social = _add_row(db, sec_op, "Pagos de aportes a la seguridad social", 3, sign="negative")
    r_pago_proveedores = _add_row(db, sec_op, "Pagos a proveedores", 4, sign="negative")
    r_pago_arriendo = _add_row(db, sec_op, "Pagos de arrendamientos", 5, sign="negative")
    r_pago_servicios = _add_row(db, sec_op, "Pagos de servicios publicos", 6, sign="negative")
    r_pago_impuestos = _add_row(db, sec_op, "Pagos de impuestos", 7, sign="negative")
    r_total_op = _add_row(db, sec_op, "Total flujos operativos", 8, row_type="subtotal")

    r_pago_activo_fijo = _add_row(db, sec_inv, "Pagos por compras de activo fijo", 0, sign="negative")
    r_cobro_activo_fijo = _add_row(db, sec_inv, "Cobros por ventas de activo fijo", 1)
    r_total_inv = _add_row(db, sec_inv, "Total flujos de inversion", 2, row_type="subtotal")

    r_pago_intereses = _add_row(db, sec_fin, "Pagos de intereses", 0, sign="negative")
    r_pago_prestamos = _add_row(db, sec_fin, "Pagos de prestamos bancarios", 1, sign="negative")
    r_pago_dividendos = _add_row(db, sec_fin, "Pagos de dividendos", 2, sign="negative")
    r_pago_acciones = _add_row(db, sec_fin, "Pagos de acciones", 3, sign="negative")
    r_cobro_intereses = _add_row(db, sec_fin, "Cobros por intereses", 4)
    r_cobro_prestamos = _add_row(db, sec_fin, "Cobros por prestamos bancarios", 5)
    r_cobro_dividendos = _add_row(db, sec_fin, "Cobros por dividendos", 6)
    r_total_fin = _add_row(db, sec_fin, "Total flujos financieros", 7, row_type="subtotal")

    r_saldo_inicial = _add_row(db, sec_res, "Dinero liquido al inicio", 0, row_type="running_balance")
    r_flujo_neto = _add_row(db, sec_res, "Flujo de caja neto", 1, row_type="total")
    r_saldo_final = _add_row(db, sec_res, "Dinero liquido al final", 2, row_type="running_balance")

    db.flush()

    r_total_op.default_projection_rule = {"type": "sum_rows", "row_ids": [
        r_cobro_contado.id, r_cobro_plazo.id, r_pago_nomina.id, r_pago_seg_social.id,
        r_pago_proveedores.id, r_pago_arriendo.id, r_pago_servicios.id, r_pago_impuestos.id,
    ]}
    r_total_inv.default_projection_rule = {"type": "sum_rows", "row_ids": [r_pago_activo_fijo.id, r_cobro_activo_fijo.id]}
    r_total_fin.default_projection_rule = {"type": "sum_rows", "row_ids": [
        r_pago_intereses.id, r_pago_prestamos.id, r_pago_dividendos.id, r_pago_acciones.id,
        r_cobro_intereses.id, r_cobro_prestamos.id, r_cobro_dividendos.id,
    ]}
    r_flujo_neto.default_projection_rule = {"type": "sum_rows", "row_ids": [r_total_op.id, r_total_inv.id, r_total_fin.id]}
    r_saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": r_saldo_final.id}
    r_saldo_final.default_projection_rule = {"type": "sum_rows", "row_ids": [r_saldo_inicial.id, r_flujo_neto.id]}

    db.flush()

    periods = sorted(sheet.periods, key=lambda p: p.sort_order)
    month1 = periods[0]

    # Month-1 values, taken verbatim from the template (its cells C14, C15, ...).
    _override(db, r_saldo_inicial.id, month1.id, 30000)
    _override(db, r_cobro_contado.id, month1.id, 15000)
    _override(db, r_cobro_plazo.id, month1.id, 3500)
    _override(db, r_pago_nomina.id, month1.id, 4500)
    _override(db, r_pago_seg_social.id, month1.id, 500)
    _override(db, r_pago_proveedores.id, month1.id, 5000)
    _override(db, r_pago_servicios.id, month1.id, 1500)
    _override(db, r_pago_impuestos.id, month1.id, 1200)
    _override(db, r_pago_intereses.id, month1.id, 350)
    _override(db, r_pago_prestamos.id, month1.id, 850)
    # Explicit zeros from the template (typed-in 0, not "no rule") -- an
    # all-empty sum_rows resolves to None, not 0, so these need a real value.
    _override(db, r_pago_arriendo.id, month1.id, 0)
    _override(db, r_pago_activo_fijo.id, month1.id, 0)
    _override(db, r_cobro_activo_fijo.id, month1.id, 0)
    _override(db, r_pago_dividendos.id, month1.id, 0)
    _override(db, r_pago_acciones.id, month1.id, 0)
    _override(db, r_cobro_intereses.id, month1.id, 0)
    _override(db, r_cobro_prestamos.id, month1.id, 0)
    _override(db, r_cobro_dividendos.id, month1.id, 0)
    db.commit()

    matrix = compute_sheet(sheet.id, db)

    op = _cells_for(matrix, r_total_op.id)
    inv = _cells_for(matrix, r_total_inv.id)
    fin = _cells_for(matrix, r_total_fin.id)
    neto = _cells_for(matrix, r_flujo_neto.id)
    inicial = _cells_for(matrix, r_saldo_inicial.id)
    final = _cells_for(matrix, r_saldo_final.id)

    expected_op = Decimal("5800")     # 15000+3500-4500-500-5000-0-1500-1200
    expected_inv = Decimal("0")
    expected_fin = Decimal("-1200")   # 0-350-850
    expected_neto = expected_op + expected_inv + expected_fin  # 4600
    expected_saldo = Decimal("30000") + expected_neto           # 34600

    assert op[0].projected_value == expected_op
    assert inv[0].projected_value == expected_inv
    assert fin[0].projected_value == expected_fin
    assert neto[0].projected_value == expected_neto
    assert inicial[0].projected_value == Decimal("30000")
    assert final[0].projected_value == expected_saldo

    # Months 2-12 have no movement in the template -- the balance must stay
    # flat, carried forward purely through previous_period chaining.
    for i in range(1, 12):
        assert final[i].projected_value == expected_saldo, f"month {i+1} saldo final"
        assert inicial[i].projected_value == expected_saldo, f"month {i+1} saldo inicial"


def test_family_budget_presupuesto_vs_real(db):
    """Recreates the category rollup of a public family-budget template
    (excelnegocios-style 'Presupuesto Familiar'): one row per spending
    category with a budgeted (projected_value) and a real (actual_value)
    amount, and the derived variance -- the exact "regla de oro: forecast
    vs. real" scenario the engine's data model was designed for.

    Figures are the template's own June-2026 example month.
    """
    sheet = CashflowSheet(user_id=TEST_USER_ID, name="Presupuesto Familiar Junio 2026",
                           currency="EUR", horizon_months=1, base_period=datetime(2026, 6, 1))
    db.add(sheet)
    db.flush()
    generate_periods(sheet, db)
    db.flush()

    sec_ingresos = SheetSection(sheet_id=sheet.id, name="Ingresos", section_type="income", sort_order=0)
    sec_gastos = SheetSection(sheet_id=sheet.id, name="Gastos por categoria", section_type="expense", sort_order=1)
    db.add_all([sec_ingresos, sec_gastos])
    db.flush()

    # (name, presupuesto, real)
    CATEGORIES = [
        ("Vivienda", Decimal("1100"), Decimal("1100")),
        ("Alimentacion", Decimal("400"), Decimal("435.80")),
        ("Suministros", Decimal("150"), Decimal("178.40")),
        ("Transporte", Decimal("80"), Decimal("80")),
        ("Educacion", Decimal("320"), Decimal("320")),
        ("Comunicaciones", Decimal("75"), Decimal("69.99")),
        ("Salud", Decimal("60"), Decimal("82.30")),
        ("Ocio", Decimal("120"), Decimal("154.60")),
        ("Ahorro", Decimal("300"), Decimal("300")),
    ]

    r_nomina = SheetRow(section_id=sec_ingresos.id, name="Nomina", sort_order=0,
                         default_projection_rule={"type": "constant", "value": 3200})
    db.add(r_nomina)
    db.flush()

    period = sheet.periods[0]
    db.add(SheetCell(row_id=r_nomina.id, period_id=period.id, actual_value=Decimal("3250")))

    category_rows = {}
    for i, (name, presupuesto, _real) in enumerate(CATEGORIES):
        row = SheetRow(section_id=sec_gastos.id, name=name, sort_order=i, sign="negative",
                        default_projection_rule={"type": "constant", "value": float(presupuesto)})
        db.add(row)
        category_rows[name] = row
    db.flush()

    for name, _presupuesto, real in CATEGORIES:
        db.add(SheetCell(row_id=category_rows[name].id, period_id=period.id, actual_value=real))
    db.commit()

    matrix = compute_sheet(sheet.id, db)

    nomina_cell = _cells_for(matrix, r_nomina.id)[0]
    assert nomina_cell.projected_value == Decimal("3200")
    assert nomina_cell.actual_value == Decimal("3250")
    assert nomina_cell.variance == Decimal("50")  # cobro mayor al presupuestado

    total_presupuesto = Decimal("0")
    total_real = Decimal("0")
    for name, presupuesto, real in CATEGORIES:
        cell = _cells_for(matrix, category_rows[name].id)[0]
        assert cell.projected_value == presupuesto, name
        assert cell.actual_value == real, name
        assert cell.variance == real - presupuesto, name
        total_presupuesto += presupuesto
        total_real += real

    # Cross-check against the template's own "Resumen" sheet totals.
    assert total_presupuesto == Decimal("2605")
    assert total_real == Decimal("2721.09")

    # Categories the template's own "Estado" column would flag "Revisar"
    # (real > presupuesto): Alimentacion, Suministros, Salud, Ocio.
    over_budget = {name for name, presupuesto, real in CATEGORIES if real > presupuesto}
    assert over_budget == {"Alimentacion", "Suministros", "Salud", "Ocio"}
