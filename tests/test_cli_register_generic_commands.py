"""Tests for `register_generic_commands` -- the generic CLI parser-registration
seam extracted so a consuming app can build its own ArgumentParser around it
instead of registering every subcommand itself. This only tests the PARSER
TREE (does everything parse? do the extension points work?), not command
behavior -- that's already covered by each command's own handler tests.
"""
import argparse

import pytest

from opencashflow.cli import GenericCommandExtensionPoints, register_generic_commands


def _build():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    ext = register_generic_commands(sub)
    return parser, ext


def test_returns_all_extension_points():
    _parser, ext = _build()
    assert isinstance(ext, GenericCommandExtensionPoints)
    for field in (
        "record_sub", "wallet_sub", "wallet_movement_sub", "wizard_sub", "sheet_sub",
        "creditcard_sub", "show_parser",
    ):
        assert getattr(ext, field) is not None


@pytest.mark.parametrize("argv", [
    ["sheets"],
    ["doctor"],
    ["sections"],
    ["section", "add", "--name", "x"],
    ["rows"],
    ["row", "add", "--section", "x", "--name", "y"],
    ["row", "edit", "--row", "x"],
    ["backfill", "--months", "3"],
    ["record", "set", "--row", "x", "--actual", "1"],
    ["record", "undo", "--row", "x"],
    ["record", "clear", "--row", "x"],
    ["override", "set", "--row", "x", "--value", "1"],
    ["override", "clear", "--row", "x"],
    ["export"],
    ["available"],
    ["period", "close", "--period", "2026-01"],
    ["wizard", "edit"],
    ["wallet", "list"],
    ["wallet", "edit", "--wallet", "x"],
    ["wallet", "movement", "add", "--wallet", "x", "--row", "y", "--amount", "1"],
    ["wallet", "movement", "undo", "--movement-id", "1"],
    ["show"],
    ["show", "--with-real", "--wallets"],
    ["creditcard", "list"],
    ["creditcard", "edit", "--card", "x"],
    ["creditcard", "cupo"],
    ["creditcard", "map", "--card", "x", "--sheet-id", "1", "--row", "y"],
])
def test_every_generic_command_parses(argv):
    parser, _ext = _build()
    parser.parse_args(argv)  # raises SystemExit on a parse error -- pytest fails loudly


@pytest.mark.parametrize("argv", [
    ["sheet"],           # no children registered yet -- required=True on sheet_sub
    ["record"],
    ["wizard"],
    ["wallet"],
    ["wallet", "movement"],
    ["creditcard"],
])
def test_split_groups_have_no_app_specific_children_yet(argv):
    # record/history, wallet/add, wallet-movement/list, wizard/new, and every
    # sheet_sub child are app-specific -- register_generic_commands alone
    # must NOT provide them (a consuming app adds them onto the extension
    # points). Calling the bare group with no verb is required=True, so it
    # should fail to parse -- this also proves sheet_sub exists with zero
    # generic children yet (Feature 2 adds import/export here later).
    parser, _ext = _build()
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_extension_points_accept_a_consuming_apps_own_children():
    _parser, ext = _build()
    ext.record_sub.add_parser("history", help="app-specific, added by a consumer")
    ext.wallet_sub.add_parser("add", help="app-specific, added by a consumer")
    ext.wallet_movement_sub.add_parser("list", help="app-specific, added by a consumer")
    ext.wizard_sub.add_parser("new", help="app-specific, added by a consumer")
    ext.sheet_sub.add_parser("create", help="app-specific, added by a consumer")
    ext.creditcard_sub.add_parser("add", help="app-specific, added by a consumer")
    ext.show_parser.add_argument("--cards", action="store_true")
    ext.show_parser.add_argument("--bridge", action="store_true")
    # Re-parsing the same argv now succeeds since the child was added.
    parser2 = argparse.ArgumentParser()
    sub2 = parser2.add_subparsers(dest="command", required=True)
    ext2 = register_generic_commands(sub2)
    ext2.wizard_sub.add_parser("new")
    parser2.parse_args(["wizard", "new"])


def test_show_parser_is_the_actual_show_parser():
    # ext.show_parser is the SAME parser "show" itself uses -- adding a flag
    # onto it must show up when parsing the top-level "show" command, not
    # just on some detached copy.
    parser, ext = _build()
    ext.show_parser.add_argument("--cards", action="store_true")
    args = parser.parse_args(["show", "--cards"])
    assert args.cards is True
