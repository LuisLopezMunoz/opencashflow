"""Example sheet generator: a Chilean-household cashflow sheet exercising
every projection rule the engine supports (constant, previous_period,
sum_rows, percent_of_row) plus a genuine running balance.

Has no notion of a ledger or of what a "user" is beyond an integer id — the
consuming app owns that model and is responsible for user_id being valid.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from opencashflow.models import CashflowSheet, CellOverride, SheetCell, SheetRow, SheetSection
from opencashflow.periods import generate_periods

SHEET_NAME = "Flujo de Caja Personal"

# Single source of truth for every number used below.
PERSONA = {
    "sueldo_liquido": 2_400_000,
    "aguinaldo_extra": 700_000,  # added to sueldo in Sep (Fiestas Patrias) & Dec (Navidad)
    "boletas_freelance": 350_000,
    "arriendo_recibido": 280_000,
    "dividendo_hipotecario": 650_000,
    "servicios_basicos": 145_000,
    "internet_celular": 55_000,
    "colegio": 280_000,
    "seguros": 85_000,
    "supermercado": 620_000,
    "supermercado_extra_dic": 160_000,
    "transporte_bencina": 180_000,
    "salud": 90_000,
    "ocio": 140_000,
    "contribuciones_cuota": 310_000,
    "permiso_circulacion": 95_000,
    "seguro_automotriz": 110_000,
    "cuota_credito_consumo": 185_000,
    "pago_tarjeta_credito": 240_000,
    "ahorro_pct": 10,
    "saldo_inicial": 1_200_000,
}


def _first_of_month(d: date) -> datetime:
    return datetime(d.year, d.month, 1)


def _add_row(db, section_id: int, name: str, sort_order: int, row_type: str = "input",
             sign: str = "positive", rule=None) -> SheetRow:
    row = SheetRow(
        section_id=section_id,
        name=name,
        row_type=row_type,
        sort_order=sort_order,
        sign=sign,
        default_projection_rule=rule,
    )
    db.add(row)
    return row


def _override(db, user_id: int, row_id: int, period_id: int, value) -> None:
    cell = db.query(SheetCell).filter(SheetCell.row_id == row_id, SheetCell.period_id == period_id).first()
    if not cell:
        cell = SheetCell(row_id=row_id, period_id=period_id)
        db.add(cell)
        db.flush()
    db.add(CellOverride(
        cell_id=cell.id,
        value=Decimal(str(value)),
        override_type="manual_value",
        created_by=user_id,
    ))


def seed_sheet(db: Session, user_id: int, months: int, base_period: date) -> CashflowSheet:
    """Create the demo planilla with a real, computed running balance.

    Two-pass construction: rows are created and flushed first so their
    database ids exist, THEN default_projection_rule is assigned on those
    same objects — sum_rows/percent_of_row/previous_period all need real
    row_ids, which don't exist until after the first flush.
    """
    sheet = CashflowSheet(
        user_id=user_id,
        name=SHEET_NAME,
        currency="CLP",
        horizon_months=months,
        base_period=_first_of_month(base_period),
    )
    db.add(sheet)
    db.flush()
    generate_periods(sheet, db)
    db.flush()

    sec_saldo = SheetSection(sheet_id=sheet.id, name="Saldo Inicial", section_type="balance", sort_order=0,
                              is_collapsible=False)
    sec_ingresos = SheetSection(sheet_id=sheet.id, name="Ingresos", section_type="income", sort_order=1)
    sec_fijos = SheetSection(sheet_id=sheet.id, name="Gastos Fijos", section_type="expense", sort_order=2)
    sec_variables = SheetSection(sheet_id=sheet.id, name="Gastos Variables", section_type="expense", sort_order=3)
    sec_impuestos = SheetSection(sheet_id=sheet.id, name="Impuestos y Patentes", section_type="expense", sort_order=4)
    sec_financiamiento = SheetSection(sheet_id=sheet.id, name="Financiamiento", section_type="financing", sort_order=5)
    sec_resumen = SheetSection(sheet_id=sheet.id, name="Resumen", section_type="balance", sort_order=6,
                                is_collapsible=False)
    db.add_all([sec_saldo, sec_ingresos, sec_fijos, sec_variables, sec_impuestos, sec_financiamiento, sec_resumen])
    db.flush()

    # --- Pass 1: create every row with no rule yet, so ids are assigned. ---
    r_saldo_inicial = _add_row(db, sec_saldo.id, "SALDO INICIAL", 0, row_type="running_balance")

    r_sueldo = _add_row(db, sec_ingresos.id, "Sueldo líquido", 0)
    r_freelance = _add_row(db, sec_ingresos.id, "Boletas freelance", 1)
    r_arriendo_in = _add_row(db, sec_ingresos.id, "Arriendo depto", 2)
    r_total_ingresos = _add_row(db, sec_ingresos.id, "Total Ingresos", 3, row_type="subtotal")

    r_dividendo = _add_row(db, sec_fijos.id, "Dividendo hipotecario", 0)
    r_servicios = _add_row(db, sec_fijos.id, "Servicios básicos", 1)
    r_internet = _add_row(db, sec_fijos.id, "Internet y celular", 2)
    r_colegio = _add_row(db, sec_fijos.id, "Colegio", 3)
    r_seguros = _add_row(db, sec_fijos.id, "Seguros", 4)
    r_total_fijos = _add_row(db, sec_fijos.id, "Total Gastos Fijos", 5, row_type="subtotal", sign="negative")

    r_super = _add_row(db, sec_variables.id, "Supermercado", 0)
    r_transporte = _add_row(db, sec_variables.id, "Transporte y bencina", 1)
    r_salud = _add_row(db, sec_variables.id, "Salud", 2)
    r_ocio = _add_row(db, sec_variables.id, "Ocio", 3)
    r_total_variables = _add_row(db, sec_variables.id, "Total Gastos Variables", 4, row_type="subtotal",
                                  sign="negative")

    r_contribuciones = _add_row(db, sec_impuestos.id, "Contribuciones", 0)
    r_permiso = _add_row(db, sec_impuestos.id, "Permiso de circulación", 1)
    r_seguro_auto = _add_row(db, sec_impuestos.id, "Seguro automotriz", 2)
    r_total_impuestos = _add_row(db, sec_impuestos.id, "Total Impuestos", 3, row_type="subtotal", sign="negative")

    r_cuota_consumo = _add_row(db, sec_financiamiento.id, "Cuota crédito de consumo", 0)
    r_pago_tarjeta = _add_row(db, sec_financiamiento.id, "Pago tarjeta de crédito", 1)
    r_total_financiamiento = _add_row(db, sec_financiamiento.id, "Total Financiamiento", 2, row_type="subtotal",
                                       sign="negative")

    r_ahorro = _add_row(db, sec_resumen.id, "Ahorro programado", 0, row_type="formula", sign="negative")
    r_imprevistos = _add_row(db, sec_resumen.id, "Imprevistos", 1)  # deliberately ruleless
    r_flujo_neto = _add_row(db, sec_resumen.id, "FLUJO NETO DEL MES", 2, row_type="total")
    r_saldo_final = _add_row(db, sec_resumen.id, "SALDO FINAL", 3, row_type="running_balance")

    db.flush()  # materialize every row id before wiring rules that reference them

    # --- Pass 2: now that ids exist, assign the rules. ---
    r_sueldo.default_projection_rule = {"type": "constant", "value": PERSONA["sueldo_liquido"]}
    r_freelance.default_projection_rule = {"type": "constant", "value": PERSONA["boletas_freelance"]}
    r_arriendo_in.default_projection_rule = {"type": "constant", "value": PERSONA["arriendo_recibido"]}
    r_total_ingresos.default_projection_rule = {
        "type": "sum_rows", "row_ids": [r_sueldo.id, r_freelance.id, r_arriendo_in.id],
    }

    r_dividendo.default_projection_rule = {"type": "constant", "value": PERSONA["dividendo_hipotecario"]}
    r_servicios.default_projection_rule = {"type": "constant", "value": PERSONA["servicios_basicos"]}
    r_internet.default_projection_rule = {"type": "constant", "value": PERSONA["internet_celular"]}
    r_colegio.default_projection_rule = {"type": "constant", "value": PERSONA["colegio"]}
    r_seguros.default_projection_rule = {"type": "constant", "value": PERSONA["seguros"]}
    r_total_fijos.default_projection_rule = {
        "type": "sum_rows",
        "row_ids": [r_dividendo.id, r_servicios.id, r_internet.id, r_colegio.id, r_seguros.id],
    }

    r_super.default_projection_rule = {"type": "constant", "value": PERSONA["supermercado"]}
    r_transporte.default_projection_rule = {"type": "constant", "value": PERSONA["transporte_bencina"]}
    r_salud.default_projection_rule = {"type": "constant", "value": PERSONA["salud"]}
    r_ocio.default_projection_rule = {"type": "constant", "value": PERSONA["ocio"]}
    r_total_variables.default_projection_rule = {
        "type": "sum_rows", "row_ids": [r_super.id, r_transporte.id, r_salud.id, r_ocio.id],
    }

    r_contribuciones.default_projection_rule = {"type": "constant", "value": 0}
    r_permiso.default_projection_rule = {"type": "constant", "value": 0}
    r_seguro_auto.default_projection_rule = {"type": "constant", "value": 0}
    r_total_impuestos.default_projection_rule = {
        "type": "sum_rows", "row_ids": [r_contribuciones.id, r_permiso.id, r_seguro_auto.id],
    }

    r_cuota_consumo.default_projection_rule = {"type": "constant", "value": PERSONA["cuota_credito_consumo"]}
    r_pago_tarjeta.default_projection_rule = {"type": "constant", "value": PERSONA["pago_tarjeta_credito"]}
    r_total_financiamiento.default_projection_rule = {
        "type": "sum_rows", "row_ids": [r_cuota_consumo.id, r_pago_tarjeta.id],
    }

    r_ahorro.default_projection_rule = {
        "type": "percent_of_row", "row_id": r_total_ingresos.id, "percent": PERSONA["ahorro_pct"],
    }
    # r_imprevistos: no rule at all — stays visibly empty ("—") in every period.
    r_flujo_neto.default_projection_rule = {
        "type": "sum_rows",
        "row_ids": [
            r_total_ingresos.id, r_total_fijos.id, r_total_variables.id,
            r_total_impuestos.id, r_total_financiamiento.id, r_ahorro.id,
        ],
    }
    # The unlock: Saldo Inicial[t] reads Saldo Final[t-1] (cross-row,
    # cross-period — never a cycle). Saldo Final[t] sums same-period.
    r_saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": r_saldo_final.id}
    r_saldo_final.default_projection_rule = {
        "type": "sum_rows", "row_ids": [r_saldo_inicial.id, r_flujo_neto.id],
    }

    db.flush()

    # --- Overrides: seed the balance chain + Chilean calendar realism. ---
    periods = sorted(sheet.periods, key=lambda p: p.sort_order)
    _override(db, user_id, r_saldo_inicial.id, periods[0].id, PERSONA["saldo_inicial"])

    for period in periods:
        m = period.period_date.month
        if m in (9, 12):  # Fiestas Patrias / Navidad
            _override(db, user_id, r_sueldo.id, period.id,
                       PERSONA["sueldo_liquido"] + PERSONA["aguinaldo_extra"])
        if m in (1, 2):  # vacaciones de verano — sin colegiatura
            _override(db, user_id, r_colegio.id, period.id, 0)
        if m == 12:  # cena de fin de año
            _override(db, user_id, r_super.id, period.id,
                       PERSONA["supermercado"] + PERSONA["supermercado_extra_dic"])
        if m in (4, 6, 9, 11):  # cuotas de contribuciones
            _override(db, user_id, r_contribuciones.id, period.id, PERSONA["contribuciones_cuota"])
        if m == 3:  # permiso de circulación + seguro automotriz se pagan en marzo
            _override(db, user_id, r_permiso.id, period.id, PERSONA["permiso_circulacion"])
            _override(db, user_id, r_seguro_auto.id, period.id, PERSONA["seguro_automotriz"])

    db.commit()
    db.refresh(sheet)
    return sheet
