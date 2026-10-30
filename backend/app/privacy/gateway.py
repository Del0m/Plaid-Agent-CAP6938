import json
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import config
from ..db.models import Account, Liability, Transaction
from ..finance import avalanche_key
from .scrub import scrub_account_name, scrub_merchant

# the only code path that turns user data into LLM prompt text (architecture section 7).
# rows are built field by field from these allowlists, never by serializing a model object,
# so a column added later can't leak by accident.

Purpose = Literal["accounts", "debts", "spending"]

ACCOUNT_FIELDS = ("name", "type", "subtype", "balance", "available", "limit")
DEBT_FIELDS = ("name", "kind", "apr", "balance", "min_payment", "next_due_date")
TXN_FIELDS = ("date", "amount", "merchant")
TXN_LIMIT = 50
DEFAULT_SPENDING_DAYS = 30
EXCLUDED_PRIMARY = frozenset({"INCOME", "TRANSFER_IN", "TRANSFER_OUT"})
CARD_PAYMENT = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
PSEUDONYM = re.compile(r"user_[0-9a-f]{4}")
_audit_lock = threading.Lock()


def _today() -> date:
    return date.today()


# cents -> dollars so the model doesn't read 41000 as forty-one thousand dollars
def _dollars(cents: Optional[int]) -> Optional[float]:
    return None if cents is None else round(cents / 100, 2)


class PrivacyGateway:
    # one instance per chat session: the pseudonym lives (and dies) with it
    # a chat session spans many requests, each with its own db session, so the caller passes
    # the session's pseudonym back in to keep it stable; omitted = start a new one
    def __init__(self, session: Session, user_id: str, audit_path: Optional[Path] = None,
                 pseudonym: Optional[str] = None):
        if pseudonym is not None and not PSEUDONYM.fullmatch(pseudonym):
            raise ValueError("pseudonym must match user_[0-9a-f]{4}")
        self._session = session
        self._user_id = user_id
        self._audit_path = audit_path or config.PROMPT_AUDIT_PATH
        self.pseudonym = pseudonym or "user_" + secrets.token_hex(2)

    def build_context(self, purpose: Purpose, *, start: Optional[date] = None,
                      end: Optional[date] = None, category: Optional[str] = None) -> str:
        if purpose == "accounts":
            data = self._accounts()
        elif purpose == "debts":
            data = self._debts()
        elif purpose == "spending":
            data = self._spending(start, end, category)
        else:
            raise ValueError(f"unknown purpose: {purpose}")

        prompt = json.dumps({"user": self.pseudonym, "purpose": purpose, "data": data},
                            separators=(",", ":"), default=str, ensure_ascii=False)
        params = {k: v for k, v in (("start", start), ("end", end), ("category", category)) if v is not None}
        self._audit(purpose, params, prompt)
        return prompt

    def _accounts(self) -> list[dict]:
        rows = self._session.scalars(
            select(Account).where(Account.user_id == self._user_id).order_by(Account.name)
        )
        return [{
            "name": scrub_account_name(a.name), "type": a.type, "subtype": a.subtype,
            "balance": _dollars(a.current_balance_cents), "available": _dollars(a.available_balance_cents),
            "limit": _dollars(a.limit_cents),
        } for a in rows]

    def _debts(self) -> list[dict]:
        rows = self._session.execute(
            select(Liability, Account)
            .join(Account, Liability.account_id == Account.id)
            .where(Liability.user_id == self._user_id)
        ).all()
        debts = [{
            "name": scrub_account_name(a.name), "kind": l.kind, "apr": l.apr, "balance": _dollars(a.current_balance_cents),
            "min_payment": _dollars(l.min_payment_cents), "next_due_date": l.next_due_date,
        } for l, a in rows]
        # same avalanche order as GET /dashboard (api/dashboard.py): highest APR first, unknown last
        debts.sort(key=lambda d: avalanche_key(d["apr"], d["balance"]))
        return debts

    # minimized per question: totals by category, plus one category's rows only when asked for
    def _spending(self, start: Optional[date], end: Optional[date], category: Optional[str]) -> dict:
        end = end or _today()
        start = start or end - timedelta(days=DEFAULT_SPENDING_DAYS - 1)
        if start > end:
            raise ValueError("start date must be on or before end date")
        category = category.strip().upper() if category else None
        inflow = category in ("INCOME", "TRANSFER_IN")
        txns = self._session.scalars(
            select(Transaction)
            .where(Transaction.user_id == self._user_id, Transaction.removed.is_(False),
                   Transaction.amount_cents < 0 if inflow else Transaction.amount_cents > 0,
                   Transaction.date >= start, Transaction.date <= end)
            .order_by(Transaction.date.desc(), Transaction.id.desc())
        ).all()

        # Aggregate spending excludes money moved between accounts and repayments of card purchases.
        # Explicit income/transfer questions are supported separately, with their direction stated.
        txns = [t for t in txns if t.pf_category_detailed != CARD_PAYMENT and (
            t.pf_category_primary not in EXCLUDED_PRIMARY or t.pf_category_primary == category)]

        totals: dict[str, int] = {}
        for t in txns:
            key = t.pf_category_primary or "UNCATEGORIZED"
            totals[key] = totals.get(key, 0) + (-t.amount_cents if inflow else t.amount_cents)

        if category is None:
            return {k: _dollars(v) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}
        # plaid categories are upper-case enums; models often send them lower-case
        matching = [t for t in txns if (t.pf_category_primary or "UNCATEGORIZED") == category]
        rows = matching[:TXN_LIMIT]
        return {
            "category": category,
            "total": _dollars(totals.get(category, 0)),   # an explicit $0, not "no data"
            "direction": "inflow" if inflow else "outflow",
            "count": len(matching), "truncated": len(matching) > TXN_LIMIT,
            "transactions": [{"date": t.date, "amount": _dollars(-t.amount_cents if inflow else t.amount_cents),
                              "merchant": None if inflow else scrub_merchant(t.merchant_name)} for t in rows],
        }

    # append-only record of exactly what left the system; pseudonym only, never the user id
    def _audit(self, purpose: str, params: dict, prompt: str) -> None:
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pseudonym": self.pseudonym, "purpose": purpose, "params": params, "prompt": prompt,
        }
        entry = (json.dumps(line, default=str, ensure_ascii=False) + "\n").encode("utf-8")
        if len(entry) > config.PROMPT_AUDIT_MAX_BYTES:
            raise ValueError("prompt exceeds the configured audit log size limit")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        with _audit_lock:
            fd = os.open(self._audit_path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                if os.fstat(fd).st_size + len(entry) > config.PROMPT_AUDIT_MAX_BYTES:
                    os.close(fd)
                    fd = None
                    self._audit_path.replace(self._audit_path.with_name(self._audit_path.name + ".1"))
                    fd = os.open(self._audit_path, flags, 0o600)
                with os.fdopen(fd, "ab") as f:
                    fd = None
                    f.write(entry)
            finally:
                if fd is not None:
                    os.close(fd)
