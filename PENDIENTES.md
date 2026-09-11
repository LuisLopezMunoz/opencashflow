# Pendientes (desarrollo)

Registro de hilos sueltos de DESARROLLO (refactors, deuda técnica de fondo) que quedaron
abiertos en el motor `opencashflow` (core, público), para no depender de la memoria de la
conversación. Se actualiza a medida que se resuelven o aparecen nuevos.

Los pendientes de la app/CLI (`opencashflow-cli`, a renombrarse a `client`) viven en su
propio `PENDIENTES.md`, no acá.

## Abiertos

_(ninguno por ahora)_

## Riesgos documentados (sin acción por ahora)

- **Colisión latente de `sort_order`** entre `periods.extend_periods_backward`
  y las guardas de orden estricto (`<`/`>`) de `close_period`/`reopen_period`
  (segunda pasada de auditoría, 2026-09-08). `extend_periods_backward`
  decrementa su `next_sort_order` en cada iteración del loop, incluso cuando
  esa iteración se salta por fecha ya existente -- bajo un setup normal
  (secuencial) nunca colisiona, pero un período insertado manualmente con un
  `sort_order` fuera de rango podría, en teoría, terminar compartiendo
  `sort_order` con otro. Ambas guardas de cierre/reapertura dependen de que
  `sort_order` sea un orden total sin colisiones. No reproducido como
  problema real; requiere un setup contrivado. Si algún día se quiere cerrar
  el gap: `UniqueConstraint` en `(sheet_id, sort_order)` (necesitaría
  migración) o una validación defensiva en el propio loop.
- **Gap de cobertura de tests preexistente** en las `UniqueConstraint` de
  `SheetCell` (`uq_cell`) y `SheetPeriod` (`uq_sheet_period`): no hay un test
  directo de que levanten `IntegrityError` a nivel de DB, a diferencia de
  `WalletMovement.reverses_movement_id`, que sí lo tiene
  (`test_reverses_movement_id_is_unique_at_the_db_level`). No introducido por
  la migración a `Mapped[]` (confirmado vía diff) -- solo una ausencia de
  test que sería barato agregar en algún momento, mismo patrón.

## Resueltos (referencia)

- **CLI: `period reopen` y `creditcard statement`** (2026-09-11). `reopen_period`
  y la proyección de interés/pago mínimo (ambas ya en el core desde la ronda
  anterior) no tenían ningún comando que las expusiera -- alcanzables solo
  importando el paquete directamente, no desde la herramienta real. Se
  agregó `opencashflow period reopen --sheet-id --period [--dry-run]
  [--balance-row]` (mismo patrón que `period close`) y
  `opencashflow creditcard statement --card [--period] [--unit]` (real o
  proyectado, con el aviso de "no confirmado contra un estado de cuenta
  real" cuando corresponde). Probado manualmente end-to-end (las tres ramas:
  saldo anterior desconocido, estado de cuenta real, proyección con saldo
  conocido) además de con tests. 304 tests en verde (7 nuevos), `ruff`/`mypy`
  limpios.

- **Segunda pasada de auditoría de deuda técnica (2026-09-08)**: se repitió
  la auditoría sobre el estado actual del core (ya con las 12 fases + los 3
  items de arriba aplicados), buscando específicamente deuda no detectada
  antes y deuda nueva introducida por ese mismo trabajo. La migración a
  `Mapped[]` y el split `compute_sheet`/`evaluate_sheet` salieron limpios
  (verificados columna por columna / comportamentalmente idénticos). Se
  encontró y corrigió un bug real en `reopen_period` (agregada en la ronda
  anterior): su `touched_notes` podía atribuirle a una reapertura el rollover
  legítimo escrito por el CIERRE DE UN PERÍODO ANTERIOR, invitando a
  borrarlo con `override clear` y destruir historial financiero real sin
  relación con la reapertura en curso -- el fix acota la query de overrides
  "tocados" a solo el período SIGUIENTE (nunca el propio), ya que un
  `period_close:`-override activo sobre el propio período que se reabre solo
  puede ser un rollover ajeno. De paso: `reopen_period` ahora verifica que el
  override que va a reemplazar realmente tenga la nota de `close_period`
  (antes lo reemplazaba a ciegas); el mensaje obsoleto de
  `record_stack.guard_periods_not_closed` ("no hay forma de reabrir un
  período cerrado") se actualizó para referenciar `reopen_period`; se cerró
  un gap real donde `cli.py`'s `override set`/`override clear` nunca
  aplicaban el guard de período cerrado (a diferencia de `record undo`/
  `record clear`, que sí lo hacían) -- confirmado con el usuario que se
  corrige aunque toque `cli.py`; y `evaluate_sheet` (la función pura del
  split anterior) ganó dos guardas de robustez para cuando la use un caller
  distinto de `compute_sheet` (orden defensivo por `sort_order`, `.get()` en
  vez de indexado directo). 297 tests en verde (6 nuevos), `ruff`/`mypy`
  limpios.

- **Migrados los modelos SQLAlchemy a estilo 2.0 (`Mapped[]`/`mapped_column()`)** (2026-09-08).
  `models.py`, `wallet.py`, `credit_card.py` -- las 10 clases ORM del core. Esto desbloqueó
  mypy de verdad: antes solo `enums.py`/`models.py` podían chequearse (~90 errores espurios
  de "Column[int] vs int" en cualquier otro archivo); ahora `[tool.mypy]` cubre las 14 fuentes
  del core (todo excepto `cli.py`/`cli_export.py`) sin errores. La migración en sí surfaceó
  ~25 gaps reales de null-safety/tipos (no espurios) que se corrigieron de paso: dos
  colisiones de nombre de variable entre loops distintos de la misma función
  (`engine.py`/`creditcard_statements.py`), un `Dict[..., object]` que debía ser
  `Dict[..., CellResult]` en `period_close.py`, un `TypedDict` para que
  `**_real_fields(...)` se pueda verificar contra el constructor de `CellResult`, un helper
  `_first_statement_period()` que documenta con un `assert` la garantía real (pero invisible
  para mypy) de que `_active_charges()` ya filtró `first_statement_period IS NULL`, y dos
  `cast()` en `sheet_spec.export_sheet_spec` donde un `str` de la base de datos ya está
  garantizado por los `@validates` del modelo a ser un valor válido de `Literal[...]`. 287
  tests en verde, sin cambios de comportamiento.

- **Separado `engine.compute_sheet` en una etapa de carga de datos (I/O) + una función de
  evaluación pura (`evaluate_sheet`)** (2026-09-08). `compute_sheet` ahora solo hace queries
  (incluida una consulta explícita de `CellOverride` en vez de depender de la relación lazy
  `cell.overrides`, para que la evaluación no toque la sesión en absoluto) y delega todo el
  cálculo -- resolución de overrides, grafo de dependencias, detección de ciclos, topo-sort,
  evaluación de reglas -- a `evaluate_sheet`, que no accede a la DB de ninguna forma. Dos
  tests nuevos en `test_engine_rules.py` prueban exactamente el objetivo original: construyen
  `SheetRow`/`SheetPeriod`/`SheetSection`/`SheetCell`/`CellOverride` directamente en Python
  (nunca persistidos, sin sesión) y llaman a `evaluate_sheet` sin ningún round-trip a SQLite.
  No se migró el resto de la suite existente (26 tests) -- ya pasaban y seguían siendo
  correctos, migrarlos no agregaba cobertura nueva. 289 tests en verde, sin cambios de
  comportamiento (verificado además contra `smoke.py`).

- **Sacado `db.commit()` de adentro de `wallet_movements.do_wallet_movement_add`/
  `do_wallet_movement_undo`** (2026-09-08) -- **corrección sobre el pendiente original: NO
  hacía falta coordinar con `opencashflow-cli`**. Al buscar los call sites reales se
  encontró que `cmd_wallet_movement_add`/`cmd_wallet_movement_undo` (lo único que de verdad
  llama a estas dos funciones) viven en el `cli.py` PÚBLICO de este mismo repo -- `ocf`
  (`opencashflow-cli`) solo importa y despacha a esos comandos genéricos, nunca llama a
  `do_wallet_movement_add`/`undo` directamente. Ambas funciones ahora solo hacen `flush()`
  (mismo criterio que `record_stack.pop_record_stack`); los dos call sites en `cli.py` ganaron
  su propio `db.commit()` explícito para preservar el comportamiento exacto de antes. Dos
  tests nuevos en `test_wallet_movements.py` prueban lo que esto habilita de verdad: dos
  movimientos compuestos en una sola transacción atómica (el ejemplo de "cuenta dividida
  entre dos billeteras" del propio docstring del módulo, antes imposible), y que si el
  segundo falla, un `db.rollback()` deshace también el primero (antes quedaba comiteado para
  siempre). 291 tests en verde, verificado además contra `smoke.py`. `opencashflow-cli`
  (pineado a v0.12.0, sin este cambio) sigue funcionando exactamente igual sin tocarse --
  esto se vuelve relevante recién cuando ese repo actualice su pin a la próxima versión.
