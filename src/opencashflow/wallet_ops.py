"""Create and edit a Wallet -- the primitives behind `wallet add`/
`wallet edit`, reusable as-is by any other consumer (an HTTP endpoint).
Moving money in or out of a wallet is a different operation with its own
module: opencashflow.wallet_movements.

Extracted out of the consuming app's CLI (wallet add, which needs that
app's own user table to pick an owner -- so this takes a plain user_id)
and out of cli.py (`_do_edit_wallet`, kept there as an alias).

`balance` is writable directly on purpose: a wallet's opening balance, or
a correction after counting the cash in it, isn't a movement on any sheet
row. Every change that IS one goes through wallet_movements instead, which
also records the row as real.
"""
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple, Union

from opencashflow.unset import UNSET, Unset
from opencashflow.wallet import Wallet

__all__ = ["UNSET", "create_wallet", "edit_wallet"]


def create_wallet(
    db, user_id: int, name: str, *, wallet_type: str = "cash", currency: str = "USD",
    balance: Decimal = Decimal("0"), description: Optional[str] = None,
) -> Wallet:
    """Create a wallet owned by `user_id`. Commits. `currency` is stored
    upper-cased (every comparison against a sheet's currency is exact)."""
    wallet = Wallet(
        user_id=user_id, name=name, wallet_type=wallet_type, currency=currency.upper(),
        balance=Decimal(str(balance)), description=description,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet


def edit_wallet(
    db, wallet: Wallet, *,
    name: Union[str, Unset] = UNSET,
    wallet_type: Union[str, Unset] = UNSET,
    currency: Union[str, Unset] = UNSET,
    balance: Union[Decimal, Unset] = UNSET,
    description: Union[Optional[str], Unset] = UNSET,
) -> Dict[str, Tuple[Any, Any]]:
    """Partial update of `wallet`: only fields actually passed (not left at
    UNSET) are considered, and only ones that actually change something are
    applied; `description=None` clears it. Commits only if something
    changed. Returns {field_name: (old_value, new_value)} for every field
    that changed."""
    requested: Dict[str, Any] = {
        "name": name,
        "wallet_type": wallet_type,
        "currency": currency.upper() if isinstance(currency, str) else currency,
        "balance": Decimal(str(balance)) if not isinstance(balance, Unset) else balance,
        "description": description,
    }
    changes: Dict[str, Tuple[Any, Any]] = {}
    for field, new in requested.items():
        if isinstance(new, Unset):
            continue
        old = getattr(wallet, field)
        if new != old:
            changes[field] = (old, new)
            setattr(wallet, field, new)
    if changes:
        db.commit()
        db.refresh(wallet)
    return changes
