from datetime import date as Date, datetime, timezone
from typing import Literal, Optional
from sqlalchemy import JSON, Double, Enum, Float, ForeignKey, UniqueConstraint
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
# one row per account, so account_id is unique and refreshing liabilities is an upsert
class Liability(Base):
    __tablename__ = "liabilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True, index=True)
    kind: Mapped[str] = mapped_column(Enum("credit", "student", "mortgage", name="liability_kind"))
    # a percentage, not money, so float is fine. Float pinned: sqlalchemy 2.1 maps float to Double, which
    # isn't what the column was created as
    apr: Mapped[Optional[float]] = mapped_column(Float)
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

# one budget per user per calendar month (architecture 6.4); fit_json keeps the months fitted and the
# categories skipped so the agent can explain the budget. null for a period opened by period close
class BudgetPeriod(Base):
    __tablename__ = "budget_periods"
    __table_args__ = (UniqueConstraint("user_id", "start_date", name="uq_budget_periods_user_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    start_date: Mapped[Date]
    end_date: Mapped[Date]
    status: Mapped[str] = mapped_column(Enum("open", "closed", name="budget_period_status"), default="open")
    fit_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    lines: Mapped[list["BudgetLine"]] = relationship(back_populates="period")

# one limit per category per period; fit_json holds the monthly totals and outlier months behind the limit
class BudgetLine(Base):
    __tablename__ = "budget_lines"
    __table_args__ = (UniqueConstraint("period_id", "category", name="uq_budget_lines_period_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("budget_periods.id"), index=True)
    category: Mapped[str]
    kind: Mapped[str] = mapped_column(Enum("fixed", "flexible", name="budget_line_kind"))
    limit_cents: Mapped[int]
    spent_cents: Mapped[int] = mapped_column(default=0)
    # a ratio, not money. Double pinned to match the migration: a bare float is Float on sqlalchemy 2.0
    last_pace: Mapped[Optional[float]] = mapped_column(Double)
    source: Mapped[str] = mapped_column(Enum("init", "adapted", "user", name="budget_line_source"))
    fit_json: Mapped[Optional[dict]] = mapped_column(JSON)

    period: Mapped["BudgetPeriod"] = relationship(back_populates="lines")

# alert and advice columns are plain strings so a new kind or action never needs a migration;
# these list the values architecture section 5 allows
AlertKind = Literal["overspend", "new_subscription", "price_increase", "income_change", "low_balance",
                    "extra_payment"]
AlertCause = Literal["one_time", "seasonal", "recurring"]
AlertAction = Literal["alert", "suggest_cuts", "adjust_next_budget", "suggest_cancel", "move_from_savings",
                      "delay_spend", "extra_payment"]
AlertStatus = Literal["pending_user", "accepted", "dismissed"]
AlertSeverity = Literal["medium", "high"]            # pace flag, over its limit
AdviceOutcome = Literal["accepted", "dismissed", "ignored"]

# something the agent wants the user to see or approve; stream_id arrives with recurring_streams (TPC-33)
class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("budget_periods.id"), index=True)
    kind: Mapped[str]
    category: Mapped[Optional[str]]
    severity: Mapped[str]
    cause: Mapped[Optional[str]]
    action: Mapped[Optional[str]]
    proposed_limit_cents: Mapped[Optional[int]]            # set by code for adjust_next_budget; close applies it
    message: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending_user")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

# what the agent told the user, read back so it doesn't repeat or contradict itself
class AdviceLog(Base):
    __tablename__ = "advice_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    category: Mapped[Optional[str]]
    advice_text: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    outcome: Mapped[Optional[str]]

# one row per graph run (budget, close, goal)
class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    graph: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[Optional[datetime]]
    status: Mapped[str]
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
