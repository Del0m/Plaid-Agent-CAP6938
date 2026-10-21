import json
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import config
from ..db.models import Account, Liability, Transaction
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
            select(Liability, Account.name)
            .join(Account, Liability.account_id == Account.id)
            .where(Liability.user_id == self._user_id)
        ).all()
        debts = [{
            "name": scrub_account_name(name), "kind": l.kind, "apr": l.apr, "balance": _dollars(l.balance_cents),
            "min_payment": _dollars(l.min_payment_cents), "next_due_date": l.next_due_date,
        } for l, name in rows]
        # same avalanche order as GET /dashboard (api/dashboard.py): highest APR first, unknown last
        debts.sort(key=lambda d: (d["apr"] is None, -(d["apr"] or 0), -(d["balance"] or 0)))
        return debts

    # minimized per question: totals by category, plus one category's rows only when asked for
    def _spending(self, start: Optional[date], end: Optional[date], category: Optional[str]) -> dict:
        end = end or _today()
        start = start or end - timedelta(days=DEFAULT_SPENDING_DAYS)
        txns = self._session.scalars(
            select(Transaction)
            .where(Transaction.user_id == self._user_id, Transaction.removed.is_(False),
                   Transaction.amount_cents > 0,          # positive = money out (sign convention)
                   Transaction.date >= start, Transaction.date <= end)
            .order_by(Transaction.date.desc(), Transaction.id.desc())
        ).all()

        totals: dict[str, int] = {}
        for t in txns:
            key = t.pf_category_primary or "UNCATEGORIZED"
            totals[key] = totals.get(key, 0) + t.amount_cents

        if category is None:
            return {k: _dollars(v) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}
        # plaid categories are upper-case enums; models often send them lower-case
        category = category.upper()
        rows = [t for t in txns if (t.pf_category_primary or "UNCATEGORIZED") == category][:TXN_LIMIT]
        return {
            "category": category,
            "total": _dollars(totals.get(category, 0)),   # an explicit $0, not "no data"
            "transactions": [{"date": t.date, "amount": _dollars(t.amount_cents),
                              "merchant": scrub_merchant(t.merchant_name)} for t in rows],
        }

    # append-only record of exactly what left the system; pseudonym only, never the user id
    def _audit(self, purpose: str, params: dict, prompt: str) -> None:
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pseudonym": self.pseudonym, "purpose": purpose, "params": params, "prompt": prompt,
        }
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
