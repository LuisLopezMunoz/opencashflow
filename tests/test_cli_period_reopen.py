"""Tests for opencashflow.cli's period-reopen CLI handler
(cmd_period_reopen) -- covers the CLI wiring itself: --sheet-id/--period/
--balance-row resolution, translating period_close.reopen_period's
ValueError into stderr+exit(1) (same convention as cmd_period_close), and
the success-path print output.

Direct ORM + direct handler calls (SimpleNamespace for args), same style as
test_cli_creditcard_core.py -- capsys captures stdout/stderr for assertions.
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow.cli import cmd_period_close, cmd_period_reopen
from opencashflow.models import Base, CashflowSheet, CellOverride, SheetCell, SheetPeriod, SheetRow, SheetSection

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def sheet(db):
    s = CashflowSheet(user_id=TEST_USER_ID, name="Reopen CLI Test Sheet", currency="CLP",
                       horizon_months=1, base_period=date(2026, 1, 1))
    db.add(s)
    db.flush()
    db.add(SheetPeriod(sheet_id=s.id, period_date=datetime(2026, 1, 1), label="Jan 2026", sort_order=0))
    db.flush()

    sec = SheetSection(sheet_id=s.id, name="Saldo", section_type="balance")
    db.add(sec)
    db.flush()
    saldo_inicial = SheetRow(section_id=sec.id, name="SALDO INICIAL", row_type="running_balance")
    saldo_final = SheetRow(section_id=sec.id, name="SALDO FINAL", row_type="running_balance")
    db.add_all([saldo_inicial, saldo_final])
    db.flush()
    saldo_inicial.default_projection_rule = {"type": "previous_period", "row_id": saldo_final.id}
    saldo_final.default_projection_rule = {"type": "sum_rows", "row_ids": [saldo_inicial.id]}
    db.commit()
    db.refresh(s)

    period = s.periods[0]
    cell = SheetCell(row_id=saldo_inicial.id, period_id=period.id)
    db.add(cell)
    db.flush()
    db.add(CellOverride(cell_id=cell.id, value=Decimal("1000000"), override_type="manual_value",
                         created_by=TEST_USER_ID))
    db.commit()
    return s


def test_reopen_via_cli_flips_is_closed_and_prints_confirmation(db, sheet, capsys):
    cmd_period_close(db, NS(
        sheet_id=sheet.id, period="2026-01", balance_row=None,
        assume_unrecorded_as_pending=False, dry_run=False, user_id=None,
    ))
    capsys.readouterr()

    period = db.query(SheetPeriod).filter(SheetPeriod.sheet_id == sheet.id).first()
    assert period.is_closed is True

    cmd_period_reopen(db, NS(sheet_id=sheet.id, period="2026-01", balance_row=None, dry_run=False))
    out = capsys.readouterr().out
    assert "[OK] Reapertura de Jan 2026" in out
    assert "sin otros rastros" in out

    db.refresh(period)
    assert period.is_closed is False


def test_reopen_via_cli_on_open_period_exits_with_error(db, sheet, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cmd_period_reopen(db, NS(sheet_id=sheet.id, period="2026-01", balance_row=None, dry_run=False))
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "not closed" in err


def test_reopen_via_cli_on_unknown_period_exits_with_error(db, sheet, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cmd_period_reopen(db, NS(sheet_id=sheet.id, period="2026-02", balance_row=None, dry_run=False))
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "no tiene un período" in err
