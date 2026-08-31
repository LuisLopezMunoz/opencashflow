# opencashflow — Glosario del Motor de Planilla

Este documento define los términos canónicos del dominio. Cualquier nombre de variable, columna o endpoint de una aplicación construida sobre este paquete debe seguir este glosario.

---

## Entidades principales

### CashflowSheet
La planilla raíz. Pertenece a un `user_id` (un entero simple: este paquete no modela usuarios ni autenticación — ver "Ownership" más abajo) y define el horizonte temporal (número de meses) y la moneda base. Una planilla **nunca** mezcla monedas; si se necesitan múltiples monedas, se usan planillas separadas.

### SheetPeriod
Cada **columna** de la planilla. Representa un mes calendario (día siempre = 1 del mes). Los períodos se generan automáticamente al crear la planilla (`opencashflow.periods.generate_periods`) según `base_period` + `horizon_months`, pero también pueden crearse manualmente para períodos históricos.

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
| `data` | Solo lectura. Derivada de datos reales externos a este paquete (ver "Ownership de los datos reales"). |
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
| `projected_value` | Lo que el motor proyecta según reglas y overrides. Es lo único que `compute_sheet()` calcula. |
| `actual_value` | Dato real (nullable hasta que la app consumidora lo escriba — ver más abajo). |
| `accrued_value` | Monto devengado/causado pero aún no pagado/cobrado. |
| `paid_value` | Monto efectivamente pagado o cobrado. |
| `pending_value` | `accrued_value - paid_value` (calculado). |
| `variance` | `actual_value - projected_value` (calculado). |

### ProjectionRule
Define **cómo calcular** el valor proyectado de una celda cuando no hay override manual. Se almacena como JSON.

**Catálogo de reglas — implementadas en V1**:

```jsonc
{ "type": "constant", "value": 700 }

{ "type": "previous_period" }
// Lee el valor de ESTA MISMA fila en el período anterior.
{ "type": "previous_period", "row_id": 42 }
// Lee el valor de OTRA fila (row_id) en el período anterior. Es lo que
// permite armar un saldo acumulado:
//   Saldo Inicial[t] = previous_period(row_id = <id de Saldo Final>)
//   Saldo Final[t]   = sum_rows([Saldo Inicial, Flujo Neto])
// Nunca crea un ciclo intra-período: siempre lee un período YA resuelto,
// sin importar si trae row_id o no.

{ "type": "sum_rows", "row_ids": [1, 2, 3] }
// Suma el valor del mismo período de cada fila listada. Cada operando se
// multiplica por el `sign` de SU PROPIA fila (positive → +1, negative → -1)
// antes de sumar, así los montos se guardan siempre en positivo y la resta
// vive en la estructura de filas, no en el dato:
//   Flujo Neto = sum_rows([Total Ingresos (sign=positive),
//                          Total Egresos  (sign=negative)])

{ "type": "percent_of_row", "row_id": 5, "percent": 10 }
// value = valor de la fila `row_id` en el MISMO período * percent / 100.
// A diferencia de previous_period, sí crea una dependencia intra-período
// y participa en la detección de ciclos.

{ "type": "empty" }
```

**Catálogo de reglas — diferidas a iteraciones posteriores** (no implementadas; un
`ProjectionRule` con uno de estos tipos, o cualquier tipo desconocido, resuelve la
celda a `null` con `effective_source="empty"` **y** `error="unsupported_rule:<type>"`,
para que se distinga de una fila que deliberadamente no tiene regla):

```jsonc
{ "type": "rolling_average", "n": 3 }
{ "type": "running_balance", "initial_balance_row_id": 99 }
{ "type": "ledger_aggregate", "ledger_mapping": { "category": "housing", "type": "expense" } }
```

### CellOverride
Registro inmutable creado cuando el usuario escribe un valor manual o asigna una regla personalizada a una celda específica. **Nunca se actualiza**; se reemplaza por uno nuevo (el anterior queda con `superseded_at` establecido). Esto garantiza trazabilidad completa de cambios. `created_by` es, igual que `CashflowSheet.user_id`, un entero simple sin FK — quien lo escribió es responsabilidad de la app consumidora.

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

## Ownership: qué es responsabilidad de este paquete y qué no

Este paquete **no tiene usuarios, autenticación, ni un ledger de transacciones reales**. Deliberadamente:

- `CashflowSheet.user_id` y `CellOverride.created_by` son columnas `Integer` simples, sin `ForeignKey`. La app que use este paquete es dueña de su propio modelo de usuario y de garantizar que esos ids sean válidos.
- `SheetRow.ledger_mapping` (JSON) es un contrato declarativo — describe *cómo* debería leerse un dato real (p. ej. `{"category": "housing", "type": "expense"}`) — pero este paquete **nunca lo interpreta ni ejecuta una consulta con él**. Poblar `actual_value`/`accrued_value`/`paid_value` a partir de datos reales (manualmente o vía un futuro "ledger bridge") es responsabilidad exclusiva de la app consumidora.
- `compute_sheet()` calcula `projected_value` a partir de reglas y overrides. `actual_value`/`accrued_value`/`paid_value` los toma tal cual estén ya guardados en la celda (o `None` si nadie los escribió todavía) y a partir de ellos sí calcula `pending_value` (`accrued_value - paid_value`) y `variance` (`actual_value - projected_value`) — ambos quedan en `None` si falta cualquiera de sus componentes, nunca se fuerza un resultado.

## Regla de oro: forecast vs. real

> **El valor real nunca sobreescribe el valor proyectado. Ambas capas coexisten.**

La planilla proyecta y explica; el valor real (poblado por la app consumidora) registra y prueba. Una celda puede mostrar un valor, pero no necesariamente es la fuente de verdad de ese valor:
- Un **override manual** es fuente de verdad directa.
- Una **celda de datos** deriva su fuente de verdad de lo que la app consumidora haya escrito en `actual_value`/`accrued_value`/`paid_value`.
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
- **Recálculo síncrono**: `compute_sheet()` recalcula todo en cada llamada. Para planillas muy grandes (> 20 períodos × 100 filas con dependencias profundas), esto puede requerir optimización futura.
- **Profundidad máxima de dependencias**: 20 niveles (`MAX_DEPENDENCY_DEPTH` en `engine.py`). Si el grafo excede este límite, el motor retorna error.
- **ComputedResult no persiste aún**: el motor devuelve resultados en memoria. La persistencia se activará en una iteración posterior.
