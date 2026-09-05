"""Smoke tests for opencashflow's own standalone CLI entry point
(opencashflow.cli.main/build_parser) -- independent of any consuming app.
Not a re-test of each handler's behavior (already covered elsewhere); this
only confirms the entry point wires seed + the generic surface together
correctly end to end, against a temp sqlite file.
"""
import subprocess
import sys


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
