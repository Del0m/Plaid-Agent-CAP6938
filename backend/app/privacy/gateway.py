import json
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import config
from ..budget_engine.diagnose import OverspendFacts
from ..budget_engine.txns import Txn, add_months
from ..db.models import Account, Liability, Transaction
from .scrub import scrub_account_name, scrub_merchant

# the only code path that turns user data into LLM prompt text (architecture section 7).
# rows are built field by field from these allowlists, never by serializing a model object,
# so a column added later can't leak by accident.

Purpose = Literal["accounts", "debts", "spending", "diagnose"]

ACCOUNT_FIELDS = ("name", "type", "subtype", "balance", "available", "limit")
DEBT_FIELDS = ("name", "kind", "apr", "balance", "min_payment", "next_due_date")
TXN_FIELDS = ("date", "amount", "merchant")
TXN_LIMIT = 50
# the budget graph's diagnose step: one flagged category's facts and its rows (spec: tpc-19)
DIAGNOSE_FIELDS = ("category", "reason", "kind", "month", "limit", "spent", "pace", "day", "days_in_month",
                   "projected", "largest", "merchants", "history", "outlier_months", "transactions",
                   "this_month_alerts", "past_advice")
# the agent's memory of a category (tpc-20): what it already alerted and what the user decided, never the text
ALERT_MEMORY_FIELDS = ("severity", "action", "status")
ADVICE_MEMORY_FIELDS = ("month", "action", "outcome")
TXN_LIMIT_DIAGNOSE = 20
DEFAULT_SPENDING_DAYS = 30


def _today() -> date:
    return date.today()


# cents -> dollars so the model doesn't read 41000 as forty-one thousand dollars
def _dollars(cents: Optional[int]) -> Optional[float]:
    return None if cents is None else round(cents / 100, 2)


def _month_name(d: date) -> str:
    return d.strftime("%B %Y")


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
                      end: Optional[date] = None, category: Optional[str] = None,
                      facts: Optional[OverspendFacts] = None, transactions: Optional[list[Txn]] = None,
                      this_month_alerts: Sequence[dict] = (), past_advice: Sequence[dict] = ()) -> str:
        if purpose == "diagnose":
            data = self._diagnose(facts, transactions or [])
            data["this_month_alerts"] = [{k: a.get(k) for k in ALERT_MEMORY_FIELDS} for a in this_month_alerts]
            data["past_advice"] = [{k: a.get(k) for k in ADVICE_MEMORY_FIELDS} for a in past_advice]
            category = facts.category
        elif purpose == "accounts":
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

    # one flagged category only (architecture section 7: a dining diagnosis sees dining). built from the facts
    # the budget engine computed, field by field; merchants scrubbed, money in dollars
    def _diagnose(self, facts: OverspendFacts, transactions: list[Txn]) -> dict:
        rows = sorted((t for t in transactions if t.category == facts.category), key=lambda t: t.date,
                      reverse=True)[:TXN_LIMIT_DIAGNOSE]
        n = len(facts.monthly_totals)
        largest = facts.largest
        return {
            "category": facts.category, "reason": facts.reason, "kind": facts.kind,
            "month": _month_name(facts.month), "limit": _dollars(facts.limit_cents),
            "spent": _dollars(facts.spent_cents),
            "pace": None if facts.pace is None else round(facts.pace, 2),
            "day": facts.days_elapsed, "days_in_month": facts.days_in_period,
            "projected": _dollars(facts.projected_cents),
            "largest": None if largest is None else {
                "date": largest.date, "amount": _dollars(largest.amount_cents),
                "merchant": scrub_merchant(largest.merchant), "share_of_spent": round(facts.largest_share, 2),
                "times_typical": None if facts.largest_vs_typical is None else round(facts.largest_vs_typical, 1)},
            "merchants": [{"merchant": scrub_merchant(m.merchant), "this_month": _dollars(m.period_cents),
                           "charges": m.charges, "months_seen_before": m.prior_months, "pattern": m.pattern,
                           "typical": _dollars(m.typical_cents)} for m in facts.merchants],
            "history": [{"month": _month_name(add_months(facts.month, i - n)), "total": _dollars(total)}
                        for i, total in enumerate(facts.monthly_totals)],
            "outlier_months": [_month_name(m) for m in facts.outlier_months],
            "transactions": [{"date": t.date, "amount": _dollars(t.amount_cents),
                              "merchant": scrub_merchant(t.merchant)} for t in rows],
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
