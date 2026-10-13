"""Tests for opencashflow.wallet_ops -- create_wallet/edit_wallet (cli.py
aliases edit_wallet as _do_edit_wallet)."""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opencashflow import cli
from opencashflow.models import Base
from opencashflow.wallet import Wallet
from opencashflow.wallet_ops import create_wallet, edit_wallet

TEST_USER_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def wallet(db):
    return create_wallet(db, TEST_USER_ID, "Cuenta corriente", wallet_type="bank", currency="clp",
                         balance=Decimal("150000"), description="BancoEstado")


def test_cli_alias_is_the_wallet_ops_function():
    assert cli._do_edit_wallet is edit_wallet


def test_create_wallet_stores_every_field_and_upper_cases_the_currency(db, wallet):
    stored = db.get(Wallet, wallet.id)
    assert (stored.user_id, stored.name, stored.wallet_type) == (TEST_USER_ID, "Cuenta corriente", "bank")
    assert (stored.currency, stored.balance, stored.description) == ("CLP", Decimal("150000"), "BancoEstado")


def test_create_wallet_defaults(db):
    w = create_wallet(db, TEST_USER_ID, "Efectivo")
    assert (w.wallet_type, w.currency, w.balance, w.description) == ("cash", "USD", Decimal("0"), None)


def test_create_wallet_never_stores_a_float_balance(db):
    w = create_wallet(db, TEST_USER_ID, "Efectivo", balance=0.1)
    assert w.balance == Decimal("0.10")


def test_edit_wallet_reports_and_applies_only_real_changes(db, wallet):
    changes = edit_wallet(db, wallet, name="Cuenta RUT", currency="clp", balance=Decimal("150000"))
    assert changes == {"name": ("Cuenta corriente", "Cuenta RUT")}


def test_edit_wallet_none_clears_the_description(db, wallet):
    assert edit_wallet(db, wallet, description=None) == {"description": ("BancoEstado", None)}


def test_edit_wallet_with_nothing_passed_changes_nothing(db, wallet):
    assert edit_wallet(db, wallet) == {}
