"""CLI-level tests for `sheet import`/`sheet export` (cmd_sheet_import/
cmd_sheet_export in opencashflow.cli), via a bare parser +
register_generic_commands -- doesn't depend on any consuming app's own
build_parser.
"""
import argparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.cli import register_generic_commands
from opencashflow.models import Base
from opencashflow.seed import seed_sheet
from datetime import date


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    register_generic_commands(sub)
    return parser.parse_args(argv)


def _dispatch(db, args):
    from opencashflow.cli import cmd_sheet_export, cmd_sheet_import
    if args.command == "sheet":
        if args.sheet_command == "import":
            cmd_sheet_import(db, args)
        elif args.sheet_command == "export":
            cmd_sheet_export(db, args)


def test_export_to_stdout_then_reimport_round_trips(db, tmp_path, capsys):
    seed_sheet(db, user_id=1, months=3, base_period=date(2026, 1, 1))
    db.commit()

    args = _parse(["sheet", "export", "--sheet-id", "1"])
    _dispatch(db, args)
    exported_text = capsys.readouterr().out
    assert "sheet:" in exported_text
    assert "SALDO FINAL" in exported_text

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(exported_text)

    db2 = sessionmaker(bind=db.get_bind())()
    args2 = _parse(["sheet", "import", "--file", str(spec_path), "--user-id", "2"])
    _dispatch(db2, args2)
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "importada" in out
    db2.close()


def test_export_to_file_infers_format_from_extension(db, tmp_path, capsys):
    seed_sheet(db, user_id=1, months=1, base_period=date(2026, 1, 1))
    db.commit()

    out_path = tmp_path / "spec.json"
    args = _parse(["sheet", "export", "--sheet-id", "1", "-o", str(out_path)])
    _dispatch(db, args)

    assert out_path.exists()
    content = out_path.read_text()
    assert content.strip().startswith("{")


def test_import_reports_error_on_duplicate_row_name(db, tmp_path, capsys):
    spec_path = tmp_path / "bad.yaml"
    spec_path.write_text(
        "sheet:\n  name: X\n  base_period: '2026-01'\n"
        "sections:\n- name: A\n  rows:\n  - name: Igual\n  - name: Igual\n"
    )
    args = _parse(["sheet", "import", "--file", str(spec_path), "--user-id", "1"])
    with pytest.raises(SystemExit):
        _dispatch(db, args)
    err = capsys.readouterr().err
    assert "Igual" in err
