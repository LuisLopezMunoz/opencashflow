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

## Uso mínimo

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
