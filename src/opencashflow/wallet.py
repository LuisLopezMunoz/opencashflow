"""A cash account (`Wallet`) a sheet owner actually holds money in -- a bank
account, cash on hand, savings -- and the append-only audit log
(`WalletMovement`) of real events that moved money into or out of one.

This is deliberately minimal: no bank integration, no reconciliation, no
transaction categorization. It's the smallest vocabulary a consuming app
needs to answer "how much cash do I really have right now", which a bare
projection engine has no concept of on its own (a sheet only knows
projected/real VALUES per cell, never where the money physically sits).

`user_id` is a plain Integer, not a ForeignKey -- same convention as
CashflowSheet.user_id (see models.py): this package takes no dependency on
a host app's own user table or its SQLAlchemy registry.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from opencashflow.models import Base


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True)
    # Plain int, not a ForeignKey -- see module docstring.
    user_id = Column(Integer, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    wallet_type = Column(String(50), nullable=False, default="cash")
    currency = Column(String(10), nullable=False, default="USD")
    balance = Column(Float, nullable=False, default=0.0)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    movements = relationship(
        "WalletMovement", back_populates="wallet", cascade="all, delete-orphan",
        foreign_keys="WalletMovement.wallet_id",
    )


class WalletMovement(Base):
    """One real-world cash event (a paycheck landing in a bank account, a
    bill paid from it) that moves a Wallet's balance AND, in the consuming
    app, records a sheet row as real -- see opencashflow.wallet_movements
    for the write logic; this is just the audit trail.

    `sheet_id`/`row_id`/`period_id` are plain ints, not ForeignKeys, even
    though the sheet-engine models (CashflowSheet, SheetRow, SheetPeriod)
    live in this SAME package/registry now -- kept this way deliberately so
    a Wallet can be created and used independently of any particular sheet
    ever existing (a consuming app is free to track cash before it has
    modeled a single sheet row). `actual_entry_id` is the id of the
    CellActualEntry this movement wrote -- used to check "is this
    movement's write still the most recent thing on that cell" before
    allowing an undo.

    `amount` is SIGNED from the wallet's own point of view (positive =
    deposit/credit, negative = withdrawal/debit) -- unlike SheetCell's
    paid_value, which is always a positive magnitude regardless of a row's
    direction. The direction is never asked for explicitly; it's derived
    from the row's own effective sign to the bottom line (see
    opencashflow.engine.effective_sign_to_top).

    Movements are an append-only audit log, same discipline as
    CellActualEntry: undoing one never deletes or edits it, it appends a
    reversal row with `reverses_movement_id` pointing back at the original.
    """
    __tablename__ = "wallet_movements"

    id = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id"), nullable=False)
    amount = Column(Float, nullable=False)

    sheet_id = Column(Integer, nullable=False)
    row_id = Column(Integer, nullable=False)
    period_id = Column(Integer, nullable=False)
    actual_entry_id = Column(Integer, nullable=False)

    reverses_movement_id = Column(Integer, ForeignKey("wallet_movements.id"), nullable=True)

    note = Column(String(255), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    wallet = relationship("Wallet", back_populates="movements", foreign_keys=[wallet_id])
