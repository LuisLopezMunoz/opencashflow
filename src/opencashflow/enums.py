"""Canonical closed-value sets for every "enum-like" string column in the
core library, in one place.

Before this module existed, each of these value sets was documented only as
a comment next to its Column(String(...)) declaration, and enforced --
inconsistently -- by up to three independent copies: cli.py's own
argparse `choices=`/interactive-wizard validation, and nothing at all in
sheet_spec.py's YAML/JSON import path (the weakest of the three entry
points into the same columns). A typo'd value slipping through the
unvalidated path doesn't raise anywhere -- it silently resolves to a wrong
answer deep inside engine.py (see row_sign_multiplier below: any sign other
than exactly "positive" resolves to -1).

Each pair below (a runtime tuple + a matching Literal alias) must stay in
sync by construction -- see test_enums.py's "no drift" test, which asserts
this via typing.get_args() rather than relying on eyeballing it. The tuple
is what @validates hooks and sheet_spec's own detection logic iterate over
at runtime; the Literal alias is what gives sheet_spec.py's Pydantic models
static type-checking value that a bare `str` field never would.
"""
from typing import Literal

ROW_SIGNS = ("positive", "negative")
RowSign = Literal["positive", "negative"]

ROW_TYPES = (
    "input", "data", "formula", "subtotal", "total", "running_balance", "label", "separator",
)
RowType = Literal[
    "input", "data", "formula", "subtotal", "total", "running_balance", "label", "separator",
]

# Every row_type except the two that represent a real, independent leaf
# obligation (input/data) -- i.e. every row_type whose value is *derived*
# from other rows rather than recorded on its own. Used by period_close.py
# to decide which rows can never carry their own "pending this period"
# amount (a formula/subtotal/total/running_balance/label/separator row is
# never something a caller records real cash against directly).
AGGREGATE_ROW_TYPES = frozenset(ROW_TYPES) - {"input", "data"}

SECTION_TYPES = ("income", "expense", "financing", "balance", "custom")
SectionType = Literal["income", "expense", "financing", "balance", "custom"]

OVERRIDE_TYPES = ("manual_value", "manual_rule", "lock")
OverrideType = Literal["manual_value", "manual_rule", "lock"]

CREDIT_CARD_STATUSES = ("active", "expired", "blocked", "cancelled")
CreditCardStatus = Literal["active", "expired", "blocked", "cancelled"]

STATEMENT_SOURCES = ("manual", "pdf_import")
StatementSource = Literal["manual", "pdf_import"]

STATEMENT_LINE_TYPES = ("charge", "payment", "credit", "interest", "fee")
StatementLineType = Literal["charge", "payment", "credit", "interest", "fee"]

# Whether a CellActualEntry is a genuine recorded value or the compensating
# entry a record_stack.pop_record_stack() undo wrote. Previously there was
# no column for this at all -- record_stack.replay_record_stack told the two
# apart ONLY by sniffing whether CellActualEntry.note happened to start with
# a magic string ("[record undo] "), with nothing anywhere reserving that
# prefix against a caller-supplied note coincidentally starting the same
# way (see MIGRATIONS.md).
ENTRY_KINDS = ("record", "undo")
EntryKind = Literal["record", "undo"]
