# Pendientes (desarrollo)

Registro de hilos sueltos de DESARROLLO (refactors, deuda técnica de fondo) que quedaron
abiertos en el motor `opencashflow` (core, público), para no depender de la memoria de la
conversación. Se actualiza a medida que se resuelven o aparecen nuevos.

Los pendientes de la app/cliente (`opencashflow-client`, ex `opencashflow-cli`) viven
en su propio `PENDIENTES.md`, no acá.

## Abiertos

- **Menor -- el mensaje del guard de período cerrado mezcla idiomas** ("Cannot registrar un
  record on…") y sugiere `period_close.reopen_period` en vez de `ocf period reopen --period
  YYYY-MM`. Llega tal cual al `detail` de un 400 de la API HTTP del client.

## Resueltos (referencia)

- **`structure.py` + validación de reglas, para la Fase 2.1 de la API HTTP** (2026-10-13,
  pendiente de release como v0.15.0).
  - **Nuevo `opencashflow.structure`**: `add_section`/`add_row`/`edit_row` salen de `cli.py`
    (que los conserva como alias `_do_add_section`/`_do_add_row`/`_do_edit_row`), y ahora
    cubren todos los campos de los DTOs (`is_collapsible`/`color_hex`, `is_visible`/
    `ledger_mapping`/`notes`). `edit_row` valida todo ANTES de tocar la fila (antes, un
    `sign` inválido dejaba el `name` ya cambiado en la sesión).
  - **`validate_rule`** -- se llama en `add_row`, `edit_row` y `overrides.set_override`
    (regla manual). Rechaza una regla que referencia filas de otra planilla o inexistentes
    (el motor las lee como nada: una suma subestimada en silencio, que `doctor` solo
    reporta después), ids que no son enteros, y un `base_rule` de `carry_forward` que no es
    un objeto -- ese caso **tumbaba `compute_sheet` entero con `AttributeError`** (fuera del
    try/except por celda del motor). El CLI nunca podía producir estas reglas (resuelve las
    filas dentro de la planilla); una API que recibe la regla como dato sí. Un tipo de
    regla desconocido se sigue aceptando a propósito: el motor lo reporta por celda.
    Rojo verificado contra v0.14.0 en un worktree descartable.
  - **`doctor`** usa el mismo `rule_row_refs`, así que ahora también ve una referencia
    rota dentro del `base_rule` de un `carry_forward`, y no se cae con un ref malformado.
  - **`schemas.py`**: `SectionCreate`/`RowCreate.sort_order` pasan de `0` a `None`
    (agregar al final, como el CLI -- un 0 fijo reordenaba la planilla); `RowUpdate`
    rechaza `null` en columnas obligatorias; cuerpos de acción nuevos
    (`RecordUndoRequest`, `RecordClearRequest`, `PeriodCloseRequest`, `PeriodReopenRequest`).

- **Fase 0 del plan de la API HTTP: `record_ops.py` + DTOs en `schemas.py`** (2026-10-12).
  - **Nuevo `opencashflow.record_ops`** (mismo patrón que `overrides.py`): `set_record`/
    `clear_record`/`undo_record`/`record_history`. Cierra un gap real: el camino de
    escritura de `record set` vivía inline dentro de `cmd_record_set` y **nunca llamaba a
    `guard_periods_not_closed`** -- se podía escribir un valor real sobre un período ya
    cerrado (test de regresión rojo antes, verde después). `undo_record` ahora levanta
    `EmptyRecordStackError` (antes capturaba `ValueError` a ciegas y re-levantaba un
    `ValueError` genérico, perdiendo el tipo). `cli.py` quedó con alias delgados
    (`_do_record_*`, mismo patrón que `_do_set_override`), sin el `sheet_id` sin uso que
    recibían undo/clear. `UNSET` es un enum de un solo miembro (tipado limpio para mypy) y
    se importa en `cli.py` como `_RECORD_UNSET`, explícitamente distinto del `_UNSET` propio
    de `cli.py`.
  - **`cmd_override_set`/`cmd_override_clear` y los 3 llamados del wizard** no capturaban
    el `ValueError` del guard de período cerrado agregado en la segunda pasada -- mostraban
    un traceback crudo (y en el wizard, tumbaban la sesión interactiva). Ahora mensaje
    limpio + exit(1) en los comandos, mensaje + volver al menú en el wizard (rojo verificado
    contra el código anterior en un worktree descartable).
  - **`schemas.py`**: `PeriodOutSimple` colapsado en `PeriodOut` (idénticos);
    `CellActualOut` eliminado (sin consumidores, reemplazado por `RecordSetResultOut`);
    `CellComputedOut` con `from_attributes=True` (valida directo sobre `engine.CellResult`);
    DTOs nuevos para Wallet/WalletMovement, CreditCard/Charge/Statement/Line,
    `InstantStatement`, `SyncReport`, los reportes de cierre/reapertura y los resultados de
    `overrides`/`record_ops` -- cada uno probado con `model_validate` sobre el valor REAL que
    devuelve la función del core que espeja. `CreditCardOut` omite `current_balance`
    (deprecado); `CreditCardCreate.last4` exige exactamente 4 dígitos en el borde.
  342 tests en verde, `ruff`/`mypy` limpios (16 fuentes). Suite de `opencashflow-client`
  (194 passed) verificada contra este core local -- compatible.

- **Migración 0003 aplicada a la base real** (2026-09-14). Backup
  `opencashflow.db.backup-2026-09-14-pre-sort-order-migration` primero,
  luego `migrations/0003_period_sort_order_unique.py` contra
  `~/.local/share/opencashflow/opencashflow.db`. Verificado: el índice
  `uq_period_sort_order` quedó creado y `opencashflow sheets` sigue
  leyendo las 21 periods de la planilla real sin cambios.

- **`creditcard statement` renombrado a `creditcard cycle`** (2026-09-14). El
  comando agregado apenas ayer chocaba de nombre con el propio subcomando
  `creditcard statement` que `opencashflow-client` ya tenía (manejo real de
  estados de cuenta con PDF) -- ambos registran un subparser sobre el mismo
  `creditcard_sub` que `register_generic_commands` comparte con cualquier
  app consumidora, y `argparse` no permite dos con el mismo nombre
  (`cannot reuse action for name ...`) -- hubiera tumbado el CLI completo
  de `client` en cuanto actualizara su pin, no solo ese comando. Se
  renombró el lado del core (recién agregado, sin publicar todavía) en vez
  del lado de `client` (ya establecido, con tests propios): función
  `cmd_creditcard_statement` → `cmd_creditcard_cycle`, subcomando
  `"statement"` → `"cycle"`. Verificado simulando el registro del propio
  subparser `"statement"` de `client` sobre el `creditcard_sub` ya
  construido del core -- ya no choca. 317 tests en verde, `ruff`/`mypy`
  limpios.

- **Los dos riesgos documentados de la segunda pasada (2026-09-11)**:
  - **Colisión de `sort_order`**: al ir a corregirlo, un repaso más riguroso mostró que
    `periods.extend_periods_backward` **no puede** producir esta colisión por sí solo --
    su pivote (`earliest`) siempre se recalcula como el `sort_order` mínimo REAL de la
    planilla al momento de la llamada, y todo valor que asigna es estrictamente menor a
    ese mínimo, así que nunca puede repetir uno ya existente, sin importar qué tan raro
    sea el dato ya presente (confirmado con un test que efectivamente prueba esto:
    `test_extend_periods_backward_pivot_always_beats_an_out_of_band_sort_order`). El
    hallazgo original de la auditoría (una función específica con un bug) no se sostuvo
    -- pero el problema de fondo que señalaba sí era real: **nada a nivel de base de
    datos** impedía que ALGÚN OTRO camino (código futuro, una edición manual) violara el
    invariante. Se cerró ahí en vez de "arreglar" una función que no tenía el bug:
    `UniqueConstraint("sheet_id", "sort_order", name="uq_period_sort_order")` nueva en
    `SheetPeriod`, aplicada vía `migrations/0003_period_sort_order_unique.py` (ver
    MIGRATIONS.md v0.13.0) -- probada contra una copia de la base real, pendiente de
    correr contra la real (ver "Abiertos").
  - **Gap de cobertura de tests**: se agregaron los tres tests que faltaban en
    `test_models.py` -- `uq_sheet_period`, `uq_period_sort_order` (el nuevo) y `uq_cell`
    levantan `IntegrityError` a nivel de DB, mismo patrón que
    `test_reverses_movement_id_is_unique_at_the_db_level`.
  308 tests en verde (4 nuevos), `ruff`/`mypy` limpios.

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
