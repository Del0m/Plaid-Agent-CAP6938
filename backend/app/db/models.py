from datetime import date as Date, datetime, timezone
from typing import Optional
from sqlalchemy import JSON, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

# Money is always integer cents (Mapped[int]), never float.
# Sign convention: amount_cents follows Plaid, positive = money leaving the account
# (see docs/architecture.md section 5).

# get current time
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

# make user table, with id, email, pass, created, and plaid items
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True)      # uuid as string
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    prefs_json: Mapped[dict] = mapped_column(JSON, default=dict)

    items: Mapped[list["PlaidItem"]] = relationship(back_populates="user")

# id of item, user owning it, and access token to read in plaid
class PlaidItem(Base):
    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    plaid_item_id: Mapped[str] = mapped_column(unique=True)
    access_token_enc: Mapped[str]                           # Fernet-encrypted, never plaintext
    institution_name: Mapped[Optional[str]]
    txn_cursor: Mapped[Optional[str]]                       # None = "not synced yet"
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="items")
    accounts: Mapped[list["Account"]] = relationship(back_populates="item")

# a bank account under a plaid item; balances can be missing from plaid so they're nullable
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id"), index=True)
    plaid_account_id: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    mask: Mapped[Optional[str]]
    type: Mapped[str]
    subtype: Mapped[Optional[str]]
    current_balance_cents: Mapped[Optional[int]]
    available_balance_cents: Mapped[Optional[int]]
    limit_cents: Mapped[Optional[int]]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    item: Mapped["PlaidItem"] = relationship(back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account")

# debt details for an account (credit card, student loan, mortgage)
class Liability(Base):
    __tablename__ = "liabilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(Enum("credit", "student", "mortgage", name="liability_kind"))
    apr: Mapped[Optional[float]]                            # a percentage, not money, so float is fine
    min_payment_cents: Mapped[Optional[int]]
    next_due_date: Mapped[Optional[Date]]
    balance_cents: Mapped[Optional[int]]

# one row per plaid transaction; plaid_txn_id is unique so re-syncing is an upsert
class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    plaid_txn_id: Mapped[str] = mapped_column(unique=True)
    date: Mapped[Date] = mapped_column(index=True)
    amount_cents: Mapped[int]
    merchant_name: Mapped[Optional[str]]
    pf_category_primary: Mapped[Optional[str]]
    pf_category_detailed: Mapped[Optional[str]]
    our_category: Mapped[Optional[str]]
    is_recurring: Mapped[bool] = mapped_column(default=False)
    pending: Mapped[bool] = mapped_column(default=False)
    removed: Mapped[bool] = mapped_column(default=False)

    account: Mapped["Account"] = relationship(back_populates="transactions")
