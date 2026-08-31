# opencashflow — Glosario del Motor de Planilla

Este documento define los términos canónicos del dominio. Cualquier nombre de variable, columna o endpoint de una aplicación construida sobre este paquete debe seguir este glosario.

---

## Entidades principales

### CashflowSheet
La planilla raíz. Pertenece a un `user_id` (un entero simple: este paquete no modela usuarios ni autenticación — ver "Ownership" más abajo) y define el horizonte temporal (número de meses) y la moneda base. Una planilla **nunca** mezcla monedas; si se necesitan múltiples monedas, se usan planillas separadas.

### SheetPeriod
Cada **columna** de la planilla. Representa un mes calendario (día siempre = 1 del mes). Los períodos se generan automáticamente al crear la planilla (`opencashflow.periods.generate_periods`) según `base_period` + `horizon_months`. También se puede extender una planilla ya creada con períodos históricos ANTERIORES al primero existente, vía `opencashflow.periods.extend_periods_backward(sheet, months, db)` — componible (llamarla varias veces da historia contigua), idempotente por fecha, y marca los períodos creados con `is_closed=True`. `opencashflow.periods.find_anchor_period(periods, today=None)` resuelve cuál período tratar como "ahora" para vistas relativas (equivalente al período que matchea el mes actual, o el futuro más cercano, o el más reciente si toda la planilla quedó en el pasado).

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

{ "type": "rolling_average", "n": 3 }
// Promedio del valor de ESTA MISMA fila en los últimos N períodos,
// caminando hacia atrás desde el período evaluado. Los períodos sin valor
// se saltan (no cuentan como 0, ni en la suma ni en el denominador) — un
// historial corto no diluye el promedio artificialmente. Si ninguno de los
// últimos N períodos tiene valor (p. ej. al principio de una planilla sin
// historial), resuelve a vacío, no a error. Igual que previous_period,
// nunca crea una dependencia intra-período — solo lee períodos YA
// resueltos, así que no participa en la detección de ciclos.

{ "type": "carry_forward", "base_rule": { "type": "constant", "value": 700 } }
// value = evaluate(base_rule, ESTE período) + max(0, pending_value de ESTA
// MISMA fila en el período ANTERIOR), donde pending_value es
// accrued_value - paid_value (solo cuando AMBOS están presentes en esa
// celda — igual que _real_fields; si no, no se arrastra nada). Solo se
// arrastra un pendiente POSITIVO — un pago en exceso (pendiente negativo)
// nunca se resta. Si no hay período anterior (primer período de la
// planilla), lo arrastrado es 0. Si base_rule no tiene dato Y no hay nada
// positivo que arrastrar, la celda entera resuelve a vacío (igual que
// cualquier otro caso "sin dato" del motor). base_rule puede ser cualquier
// otra regla EXCEPTO carry_forward — anidar se rechaza con
// error="unsupported_rule:carry_forward(nested)" en vez de recursar.
// Igual que previous_period y rolling_average, el propio salto al período
// anterior NUNCA crea una dependencia intra-período — solo las
// dependencias del MISMO período que traiga base_rule (p. ej. si
// base_rule es sum_rows o percent_of_row) sí se registran.

{ "type": "empty" }
```

**Catálogo de reglas — diferidas a iteraciones posteriores** (no implementadas; un
`ProjectionRule` con uno de estos tipos, o cualquier tipo desconocido, resuelve la
celda a `null` con `effective_source="empty"` **y** `error="unsupported_rule:<type>"`,
para que se distinga de una fila que deliberadamente no tiene regla):

```jsonc
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
| `lock` | El valor calculado se congela para que no cambie aunque cambie la regla. El motor solo respeta el `value` ya guardado en el override (lo trata igual que `manual_value`) — **capturar** ese valor (correr `compute_sheet()` y guardar lo que haya dado en ese momento) es responsabilidad de la app consumidora al crear el override, no algo que este paquete haga por sí solo. |

Solo el override con `superseded_at = NULL` está vigente.

### CellActualEntry
Log de solo-inserción (append-only) de cada escritura a la capa real de una celda
(`actual_value`/`accrued_value`/`paid_value`). A diferencia de `CellOverride`, el motor
**nunca** necesita resolver "cuál es el vigente" — `SheetCell.actual_value`/`accrued_value`/`paid_value`
son siempre la única fuente que `compute_sheet()` lee. Esta tabla existe solo para que una
corrección a un valor real ya registrado no borre el rastro de cuál era antes: cada escritura
inserta una fila nueva con el estado completo resultante de los tres campos (una foto, no un
delta), nunca se actualiza una fila existente. `created_by` sigue el mismo criterio que el
resto del paquete: entero simple, sin `ForeignKey`.

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

3. ¿Hay CellOverride vigente con override_type = lock?
       → usa override.value (congelado). STOP.

4. ¿La fila tiene default_projection_rule?
       → evalúa esa regla.

5. Sin regla aplicable
       → effective_value = NULL (vacío).
```

---

## Restricciones de V1

- **Solo granularidad mensual**: los períodos son siempre el primer día de cada mes.
- **Moneda única por planilla**: no se mezclan monedas dentro de una misma planilla para que `sum_rows` y `running_balance` sean coherentes.
- **Recálculo síncrono**: `compute_sheet()` recalcula todo en cada llamada. Para planillas muy grandes (> 20 períodos × 100 filas con dependencias profundas), esto puede requerir optimización futura.
- **Profundidad máxima de dependencias**: 20 niveles (`MAX_DEPENDENCY_DEPTH` en `engine.py`). Si el grafo excede este límite, el motor retorna error.
- **ComputedResult no persiste aún**: el motor devuelve resultados en memoria. La persistencia se activará en una iteración posterior.
