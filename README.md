# opencashflow

Motor de proyección de flujo de caja: modelos SQLAlchemy, engine de cálculo y schemas
Pydantic. Agnóstico de framework web, de autenticación y de ledger — es una librería,
no una aplicación.

Modela una planilla financiera como sección → fila → celda, donde cada fila declara una
regla de proyección (`constant`, `previous_period`, `sum_rows`, `percent_of_row`, entre
otras) y cada celda puede exponer un valor proyectado y, por separado, un valor real
(`actual_value`/`accrued_value`/`paid_value`) que la aplicación consumidora es responsable
de poblar. Overrides manuales son inmutables y auditables (nunca se actualizan, se
reemplazan). Ver [docs/model.md](docs/model.md) para el glosario completo del dominio.

## Instalación

```bash
pip install "opencashflow @ git+https://github.com/LuisLopezMunoz/opencashflow.git@v0.1.0"
```

(Todavía no está publicado en PyPI — instalar directamente desde el repositorio.)

Esto también instala el comando `opencashflow` (equivalente a `python -m
opencashflow.cli`) -- una CLI standalone, independiente de cualquier app consumidora
(sin ledger, sin auth multiusuario), útil para probar la librería o correr el demo de
abajo.

## Demo en un minuto

```bash
opencashflow seed --user-id 1 --months 12 --base-period 2026-01
opencashflow rows --sheet-id 1
opencashflow doctor --sheet-id 1
opencashflow show --sheet-id 1
```

Esto crea (en una base SQLite descartable, `./opencashflow-demo.db` por defecto) una
planilla de ejemplo realista -- un hogar chileno con ingresos, gastos fijos/variables,
impuestos, financiamiento y un saldo acumulado -- lista sus filas y reglas, y por
último muestra la matriz calculada como tabla. (`--cards`/`--bridge` -- tarjetas de
crédito con parseo de PDF de banco y financiamiento puente -- son de las pocas cosas
que se quedan del lado de una app consumidora real; el modelo básico de tarjeta y
`creditcard list/edit/cupo/map` sí son parte de esta CLI standalone. Ver
[docs/model.md](docs/model.md), secciones "CLI genérico" y "Tarjetas de crédito".)

El mismo ejemplo vive como archivo declarativo en
[docs/examples/hogar-chileno.yaml](docs/examples/hogar-chileno.yaml) -- una planilla
completa (secciones → filas → reglas) en un solo documento YAML, generado con `sheet
export` y reimportable con `sheet import`:

```bash
opencashflow sheet import --file docs/examples/hogar-chileno.yaml --user-id 1
opencashflow sheet export --sheet-id 1   # -- lo mismo, de vuelta a YAML, a stdout
```

Ver [docs/model.md](docs/model.md), sección "Sheet spec", para el formato completo
(los 6 tipos de regla soportados, cómo se referencian filas por nombre, `sheet
import`/`export`). Nota: el spec captura la ESTRUCTURA de una planilla (secciones,
filas, reglas) -- no overrides manuales por celda, que es justamente lo que distingue
al ejemplo de arriba (generado con `seed`, que sí aplica un par de overrides realistas
de calendario) del mismo ejemplo reimportado desde el YAML (que empieza sin ellos).

## Uso mínimo (desde Python)

Este paquete no crea usuarios ni conexiones a base de datos por ti — trae tu propia
`Session` de SQLAlchemy y tu propio `user_id` (un entero simple, sin relación con ningún
modelo de usuario: ver "Ownership" en [docs/model.md](docs/model.md)).

```python
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.engine import compute_sheet
from opencashflow.models import Base, CashflowSheet, SheetRow, SheetSection
from opencashflow.periods import generate_periods

engine = create_engine("sqlite:///example.db")
Base.metadata.create_all(bind=engine)
db = sessionmaker(bind=engine)()

sheet = CashflowSheet(user_id=1, name="Mi flujo de caja", currency="CLP",
                       horizon_months=6, base_period=datetime(2026, 1, 1))
db.add(sheet)
db.flush()
generate_periods(sheet, db)

section = SheetSection(sheet_id=sheet.id, name="Ingresos", section_type="income")
db.add(section)
db.flush()

row = SheetRow(section_id=section.id, name="Sueldo",
               default_projection_rule={"type": "constant", "value": 1_500_000})
db.add(row)
db.commit()

result = compute_sheet(sheet.id, db)
```

Para un ejemplo completo y realista (una planilla de hogar chileno con las cuatro reglas
de proyección, saldo acumulado y overrides de calendario), ver
`opencashflow.seed.seed_sheet()`.

## Desarrollo

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Licencia

Apache 2.0 — ver [LICENSE](LICENSE).
