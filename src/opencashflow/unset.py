"""The one "this argument was not provided -- leave it untouched" sentinel
shared by every partial-write function in this package (record_ops,
structure, wallet_ops, creditcard_ops). Distinct from an explicit None,
which for a nullable field is a real value meaning "clear it".

A single-member enum rather than a bare object() so it type-checks: a
parameter annotated `Union[str, Unset]` lets mypy narrow it with
isinstance(value, Unset).
"""
import enum


class Unset(enum.Enum):
    TOKEN = 0


UNSET = Unset.TOKEN
