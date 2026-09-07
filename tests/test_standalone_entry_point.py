"""Smoke tests for opencashflow's own standalone CLI entry point
(opencashflow.cli.main/build_parser) -- independent of any consuming app.
Not a re-test of each handler's behavior (already covered elsewhere); this
only confirms the entry point wires seed + the generic surface together
correctly end to end, against a temp sqlite file.

`test_show_against_a_seeded_sheet`/`test_creditcard_*_dispatch_through_main`
exist for the same reason as opencashflow-cli's own
test_main_dispatch_*.py family: every OTHER test of cmd_show/
cmd_creditcard_* calls the handler directly, which proves it works but
never proves main()'s own flat if/elif chain actually routes "show"/
"creditcard ..." to it -- this module's main() gained both branches in the
same change that added these commands to register_generic_commands, so
they get the same subprocess-based dispatch coverage.
"""
import subprocess
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.credit_card import CreditCard
from opencashflow.models import Base


def _run(tmp_path, *args):
    db_url = f"sqlite:///{tmp_path}/test.db"
    result = subprocess.run(
        [sys.executable, "-m", "opencashflow.cli", "--db-url", db_url, *args],
        capture_output=True, text=True,
    )
    return result


def test_seed_then_sheets_then_rows(tmp_path):
    seed = _run(tmp_path, "seed", "--user-id", "1", "--months", "3")
    assert seed.returncode == 0, seed.stderr
    assert "[OK]" in seed.stdout

    sheets = _run(tmp_path, "sheets")
    assert sheets.returncode == 0, sheets.stderr
    assert "Flujo de Caja Personal" in sheets.stdout

    rows = _run(tmp_path, "rows", "--sheet-id", "1")
    assert rows.returncode == 0, rows.stderr
    assert "SALDO INICIAL" in rows.stdout


def test_record_set_against_a_seeded_sheet(tmp_path):
    _run(tmp_path, "seed", "--user-id", "1", "--months", "3")
    result = _run(tmp_path, "record", "set", "--row", "Sueldo", "--actual", "100", "--sheet-id", "1")
    assert result.returncode == 0, result.stderr
    assert "[OK]" in result.stdout


def test_no_command_shows_usage_error():
    result = subprocess.run(
        [sys.executable, "-m", "opencashflow.cli"], capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_db_url_env_var_is_honored(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/env.db"
    monkeypatch.setenv("OPENCASHFLOW_DB_URL", db_url)
    result = subprocess.run(
        [sys.executable, "-m", "opencashflow.cli", "seed", "--user-id", "1", "--months", "1"],
        capture_output=True, text=True, env={**__import__("os").environ, "OPENCASHFLOW_DB_URL": db_url},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "env.db").exists()


def test_show_against_a_seeded_sheet_dispatches_through_main(tmp_path):
    _run(tmp_path, "seed", "--user-id", "1", "--months", "3")
    result = _run(tmp_path, "show", "--sheet-id", "1")
    assert result.returncode == 0, result.stderr
    assert "SALDO INICIAL" in result.stdout


def test_creditcard_list_edit_cupo_map_dispatch_through_main(tmp_path):
    _run(tmp_path, "seed", "--user-id", "1", "--months", "3")

    # No standalone `creditcard add` (same reason there's no standalone
    # `wallet add` -- see main module's own "Entry point" comment): the
    # card is created directly via the ORM, against the SAME sqlite file
    # the subprocess calls below read/write.
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    card = CreditCard(
        user_id=1, name="Tarjeta Standalone", bank="Banco Demo", credit_limit=500_000.0,
        current_balance=0.0, currency="CLP", closing_day=25, due_day=5, interest_rate=0.1,
    )
    session.add(card)
    session.commit()
    session.close()

    list_result = _run(tmp_path, "creditcard", "list")
    assert list_result.returncode == 0, list_result.stderr
    assert "Tarjeta Standalone" in list_result.stdout

    edit_result = _run(tmp_path, "creditcard", "edit", "--card", "Tarjeta Standalone", "--bank", "Banco Nuevo")
    assert edit_result.returncode == 0, edit_result.stderr
    assert "bank: Banco Demo -> Banco Nuevo" in edit_result.stdout

    rows_result = _run(tmp_path, "rows", "--sheet-id", "1")
    assert rows_result.returncode == 0, rows_result.stderr
    assert "Pago tarjeta de crédito" in rows_result.stdout

    map_result = _run(
        tmp_path, "creditcard", "map", "--card", "Tarjeta Standalone", "--sheet-id", "1",
        "--row", "Pago tarjeta de crédito",
    )
    assert map_result.returncode == 0, map_result.stderr
    assert "planilla #1" in map_result.stdout

    cupo_result = _run(tmp_path, "creditcard", "cupo", "--card", "Tarjeta Standalone")
    assert cupo_result.returncode == 0, cupo_result.stderr
    assert "Tarjeta Standalone" in cupo_result.stdout
