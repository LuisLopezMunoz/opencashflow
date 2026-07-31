# OpenCashFlow — Glosario del Motor de Planilla

Este documento define los términos canónicos del dominio. Cualquier nombre de variable, columna o endpoint debe seguir este glosario.

---

## Entidades principales

### CashflowSheet
La planilla raíz. Pertenece a un usuario y define el horizonte temporal (número de meses) y la moneda base. Una planilla **nunca** mezcla monedas; si se necesitan múltiples monedas, se usan planillas separadas.

### SheetPeriod
Cada **columna** de la planilla. Representa un mes calendario (día siempre = 1 del mes). Los períodos se generan automáticamente al crear la planilla según `base_period` + `horizon_months`, pero también pueden crearse manualmente para períodos históricos.

- **Período futuro**: `period_date > hoy`. Muestra únicamente el valor proyectado.
- **Período actual**: `period_date` cae en el mes en curso. Muestra proyectado y real en paralelo.
- **Período cerrado**: `period_date < primer día del mes actual`. Muestra el valor real; el proyectado se preserva para comparación.

### SheetSection
Agrupador visual de filas. Ejemplos: "Ingresos", "Gastos Fijos", "Gastos Variables", "Financiamiento", "Saldo". Cada sección pertenece a exactamente una planilla.

### SheetRow
Cada **fila** de la planilla: un concepto financiero ("Sueldo", "Arriendo", "Luz", "Saldo final"). Una fila pertenece a una sección y define su regla de proyección por defecto (`default_projection_rule`).

**Tipos de fila (`row_type`)**:

| Tipo | Comportamiento |
|---|---|
| `input` | Celda editable. Valor efectivo = override manual > regla > vacío. |
| `data` | Solo lectura. Derivada del ledger (transacciones reales). |
| `formula` | Resultado de una regla de cálculo declarada. Solo lectura por defecto; admite override con `lock`. |
| `subtotal` | Suma automática de las filas de una sección. |
| `total` | Suma de subtotales/secciones. |
| `running_balance` | Saldo acumulado. Arranca de un saldo inicial y suma/resta cada fila definida. |
| `label` | Solo texto, sin valor numérico. |
| `separator` | Línea divisoria visual, sin valor numérico. |

### SheetCell
La **intersección** de una fila y un período: `(row_id, period_id)`. Es la unidad de datos de la planilla.

Las celdas **no se crean todas al inicializar la planilla**; se materializan bajo demanda cuando el motor las calcula o el usuario escribe un override.

Una celda puede exponer hasta cinco valores distintos:

| Campo | Significado |
|---|---|
| `projected_value` | Lo que el motor proyecta según reglas y overrides. |
| `actual_value` | Dato real extraído del ledger (nullable hasta que existan transacciones). |
| `accrued_value` | Monto devengado/causado pero aún no pagado/cobrado. |
| `paid_value` | Monto efectivamente pagado o cobrado según el ledger. |
| `pending_value` | `accrued_value - paid_value` (calculado). |
| `variance` | `actual_value - projected_value` (calculado). |

### ProjectionRule
Define **cómo calcular** el valor proyectado de una celda cuando no hay override manual. Se almacena como JSON.

**Catálogo de reglas**:

```jsonc
{ "type": "constant", "value": 700 }
{ "type": "previous_period" }
{ "type": "rolling_average", "n": 3 }
{ "type": "sum_rows", "row_ids": [1, 2, 3] }
{ "type": "running_balance", "initial_balance_row_id": 99 }
{ "type": "ledger_aggregate", "ledger_mapping": { "category": "housing", "type": "expense" } }
{ "type": "percent_of_row", "row_id": 5, "percent": 10 }
{ "type": "empty" }
```

### CellOverride
Registro inmutable creado cuando el usuario escribe un valor manual o asigna una regla personalizada a una celda específica. **Nunca se actualiza**; se reemplaza por uno nuevo (el anterior queda con `superseded_at` establecido). Esto garantiza trazabilidad completa de cambios.

**Tipos de override (`override_type`)**:

| Tipo | Descripción |
|---|---|
| `manual_value` | El usuario ingresó un número directamente. |
| `manual_rule` | El usuario reemplazó la regla de la fila solo para esta celda. |
| `lock` | El usuario bloqueó el valor calculado para que no cambie aunque cambie la regla. |

Solo el override con `superseded_at = NULL` está vigente.

### CellDependency
Arista en el grafo de dependencias entre filas. Permite ordenar topológicamente el recálculo y detectar ciclos antes de evaluar.

- `source_period_offset = 0` → misma columna (puede crear ciclos si no se controla).
- `source_period_offset = -1` → columna anterior (nunca crea ciclos).

### ComputedResult
Caché persistente del valor efectivo final de una celda, con traza de qué fuente ganó. En la primera iteración el motor calcula en memoria y no persiste en esta tabla. Se activará en iteraciones posteriores para auditoría e historial.

---

## Regla de oro: forecast vs. real

> **El ledger nunca sobreescribe el valor proyectado. Ambas capas coexisten.**

La planilla proyecta y explica; el ledger registra y prueba.

Una celda puede mostrar un valor, pero no necesariamente es la fuente de verdad de ese valor:
- Un **override manual** es fuente de verdad directa.
- Una **celda de datos** deriva su fuente de verdad del ledger.
- Una **celda de saldo** deriva del historial de movimientos de billeteras.
- Una **celda de proyección** deriva de reglas.

---

## Prioridad de resolución del valor efectivo

```
1. ¿Hay CellOverride vigente con override_type = manual_value?
       → usa override.value. STOP.

2. ¿Hay CellOverride vigente con override_type = manual_rule?
       → evalúa override.custom_rule. STOP.

3. ¿La fila tiene default_projection_rule?
       → evalúa esa regla.

4. Sin regla aplicable
       → effective_value = NULL (vacío).
```

---

## Restricciones de V1

- **Solo granularidad mensual**: los períodos son siempre el primer día de cada mes.
- **Moneda única por planilla**: no se mezclan monedas dentro de una misma planilla para que `sum_rows` y `running_balance` sean coherentes.
- **Recálculo síncrono**: el motor recalcula en el mismo request. Para planillas muy grandes (> 20 períodos × 100 filas con dependencias profundas), esto puede requerir optimización futura.
- **Profundidad máxima de dependencias**: 20 niveles. Si el grafo excede este límite, el motor retorna error.
- **ComputedResult no persiste aún**: el motor devuelve resultados en memoria. La persistencia se activará en una iteración posterior.
