import logging
from datetime import date as Date
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db.models import Account, Liability, PlaidItem, Transaction
from ..security import decrypt
from . import client
from .client import PlaidError

# pulls an item's accounts, transactions, balances and liabilities from plaid into our db.
# every write is an upsert keyed on a plaid id, so running any of these twice changes nothing.

logger = logging.getLogger(__name__)

# plaid's max page size for /transactions/sync
SYNC_PAGE_SIZE = 500
# times to restart pagination when plaid's data changes underneath us
MAX_SYNC_RESTARTS = 3
# /liabilities/get errors that just mean "this bank has no debts for us", not a failure
NO_LIABILITIES_CODES = {"PRODUCTS_NOT_SUPPORTED", "NO_LIABILITY_ACCOUNTS"}


# dollars (float) to integer cents; round, because int(12.29 * 100) == 1228
def _to_cents(amount: Optional[float]) -> Optional[int]:
    return None if amount is None else round(amount * 100)


def _to_date(value: Optional[str]) -> Optional[Date]:
    return None if value is None else Date.fromisoformat(value)


# insert or update account rows from any plaid response's "accounts" list;
# returns plaid_account_id -> our row so callers can fill foreign keys
def _upsert_accounts(session: Session, item: PlaidItem, plaid_accounts: list[dict]) -> dict[str, Account]:
    ids = [a["account_id"] for a in plaid_accounts]
    existing = {
        a.plaid_account_id: a
        for a in session.scalars(select(Account).where(Account.plaid_account_id.in_(ids)))
    }
    for pa in plaid_accounts:
        acct = existing.get(pa["account_id"])
        if acct is None:
            acct = Account(user_id=item.user_id, item_id=item.id, plaid_account_id=pa["account_id"])
            session.add(acct)
            existing[pa["account_id"]] = acct
        balances = pa.get("balances") or {}
        acct.name = pa["name"]
        acct.mask = pa.get("mask")
        acct.type = pa["type"]
        acct.subtype = pa.get("subtype")
        acct.current_balance_cents = _to_cents(balances.get("current"))
        acct.available_balance_cents = _to_cents(balances.get("available"))
        acct.limit_cents = _to_cents(balances.get("limit"))
    session.flush()  # new accounts get their ids before transactions point at them
    return existing


# every page from the item's cursor to the end; nothing is written here, so a restart is free
def _fetch_sync_pages(access_token: str, start_cursor: Optional[str]) -> dict:
    for attempt in range(MAX_SYNC_RESTARTS):
        result = {"added": [], "modified": [], "removed": [], "accounts": {}}
        cursor = start_cursor
        try:
            while True:
                body = {"access_token": access_token, "count": SYNC_PAGE_SIZE}
                if cursor:
                    body["cursor"] = cursor
                page = client.plaid_post("/transactions/sync", body)
                result["added"] += page["added"]
                result["modified"] += page["modified"]
                result["removed"] += page["removed"]
                for a in page["accounts"]:
                    result["accounts"][a["account_id"]] = a
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    result["next_cursor"] = cursor
                    result["update_status"] = page.get("transactions_update_status")
                    return result
        except PlaidError as err:
            # plaid's data changed mid-loop: throw away every page and start over from start_cursor
            if err.error_code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise
            logger.info("transactions changed during pagination, restarting (attempt %d)", attempt + 1)
    raise RuntimeError(f"/transactions/sync kept changing after {MAX_SYNC_RESTARTS} restarts")


# loop /transactions/sync until has_more is false, upsert added/modified, mark removed, save cursor.
# rows and cursor commit together, so a crash before the commit just replays the same pages next time.
def sync_transactions(session: Session, item: PlaidItem) -> dict:
    pages = _fetch_sync_pages(decrypt(item.access_token_enc), item.txn_cursor)
    _upsert_accounts(session, item, list(pages["accounts"].values()))
    # every account on the item, not just ones in this response, in case a txn's account was synced earlier
    accounts = {
        a.plaid_account_id: a
        for a in session.scalars(select(Account).where(Account.item_id == item.id))
    }

    upserts = pages["added"] + pages["modified"]
    ids = [t["transaction_id"] for t in upserts] + [r["transaction_id"] for r in pages["removed"]]
    existing = {
        t.plaid_txn_id: t
        for t in session.scalars(select(Transaction).where(Transaction.plaid_txn_id.in_(ids)))
    }

    for pt in upserts:
        txn = existing.get(pt["transaction_id"])
        if txn is None:
            txn = Transaction(user_id=item.user_id, plaid_txn_id=pt["transaction_id"])
            session.add(txn)
            existing[pt["transaction_id"]] = txn
        pfc = pt.get("personal_finance_category") or {}
        txn.account_id = accounts[pt["account_id"]].id
        txn.date = _to_date(pt["date"])
        txn.amount_cents = _to_cents(pt["amount"])     # plaid's sign kept: positive = money out
        txn.merchant_name = pt.get("merchant_name")
        txn.pf_category_primary = pfc.get("primary")
        txn.pf_category_detailed = pfc.get("detailed")
        txn.pending = pt.get("pending", False)
        txn.removed = False

    # soft delete: keep the row for history. a pending txn that posts arrives as removed
    # (old pending id) plus added (new posted id). ids we never stored are skipped.
    for r in pages["removed"]:
        txn = existing.get(r["transaction_id"])
        if txn is not None:
            txn.removed = True

    item.txn_cursor = pages["next_cursor"]
    session.commit()
    return {
        "added": len(pages["added"]),
        "modified": len(pages["modified"]),
        "removed": len(pages["removed"]),
        "update_status": pages["update_status"],
    }


# real-time balances for every account on the item
def refresh_balances(session: Session, item: PlaidItem) -> int:
    data = client.plaid_post("/accounts/balance/get", {"access_token": decrypt(item.access_token_enc)})
    _upsert_accounts(session, item, data["accounts"])
    session.commit()
    return len(data["accounts"])


# the one APR we store per credit card: purchase APR, else whatever plaid lists first
def _credit_apr(aprs: list[dict]) -> Optional[float]:
    for apr in aprs:
        if apr.get("apr_type") == "purchase_apr":
            return apr.get("apr_percentage")
    return aprs[0].get("apr_percentage") if aprs else None


# plaid's three liability shapes flattened to (kind, plaid_account_id, apr, min_payment, due_date)
def _flatten_liabilities(liabs: dict) -> list[tuple]:
    rows = []
    for c in liabs.get("credit") or []:
        rows.append(("credit", c["account_id"], _credit_apr(c.get("aprs") or []),
                     c.get("minimum_payment_amount"), c.get("next_payment_due_date")))
    for s in liabs.get("student") or []:
        rows.append(("student", s["account_id"], s.get("interest_rate_percentage"),
                     s.get("minimum_payment_amount"), s.get("next_payment_due_date")))
    for m in liabs.get("mortgage") or []:
        rows.append(("mortgage", m["account_id"], (m.get("interest_rate") or {}).get("percentage"),
                     m.get("next_monthly_payment"), m.get("next_payment_due_date")))
    return rows


# /liabilities/get, upserted one row per account; banks without liabilities are skipped, not errors
def refresh_liabilities(session: Session, item: PlaidItem) -> list[Liability]:
    try:
        data = client.plaid_post("/liabilities/get", {"access_token": decrypt(item.access_token_enc)})
    except PlaidError as err:
        if err.error_code in NO_LIABILITIES_CODES:
            logger.info("item %s has no liabilities (%s)", item.id, err.error_code)
            return []
        raise

    accounts = _upsert_accounts(session, item, data["accounts"])
    acct_ids = [a.id for a in accounts.values()]
    existing = {
        l.account_id: l
        for l in session.scalars(select(Liability).where(Liability.account_id.in_(acct_ids)))
    }

    for kind, plaid_account_id, apr, min_payment, due in _flatten_liabilities(data["liabilities"]):
        acct = accounts[plaid_account_id]
        liab = existing.get(acct.id)
        if liab is None:
            liab = Liability(user_id=item.user_id, account_id=acct.id)
            session.add(liab)
            existing[acct.id] = liab
        liab.kind = kind
        liab.apr = apr
        liab.min_payment_cents = _to_cents(min_payment)
        liab.next_due_date = _to_date(due)
        liab.balance_cents = acct.current_balance_cents

    session.commit()
    return list(existing.values())


# everything for one item: transactions, then fresh balances, then liabilities
def sync_item(session: Session, item: PlaidItem) -> dict:
    txns = sync_transactions(session, item)
    n_accounts = refresh_balances(session, item)
    liabilities = refresh_liabilities(session, item)
    return {
        "transactions": txns,
        "accounts": n_accounts,
        "liabilities": [{"kind": l.kind, "apr": l.apr} for l in liabilities],
    }
