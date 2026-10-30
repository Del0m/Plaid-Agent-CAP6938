from datetime import date
from itertools import count

from app.db.models import Account, Liability, PlaidItem, Transaction, User

# row builders shared by tests that seed a user's finances straight into the db (no plaid calls)

# one counter for the whole test run: plaid_txn_id is unique across sessions, not just within one
_txn_ids = count()


def add_user(s, user_id, email):
    s.add(User(id=user_id, email=email, password_hash="!"))
    item = PlaidItem(user_id=user_id, plaid_item_id=f"item-{user_id}", access_token_enc="x")
    s.add(item)
    s.flush()
    return item


def add_account(s, item, name, type_="depository", balance=10000, mask=None, plaid_id=None, liability=None):
    acct = Account(user_id=item.user_id, item_id=item.id, plaid_account_id=plaid_id or f"acc-{item.id}-{name}",
                   name=name, mask=mask, type=type_, current_balance_cents=balance)
    s.add(acct)
    s.flush()
    if liability:
        s.add(Liability(user_id=item.user_id, account_id=acct.id, balance_cents=balance, **liability))
    return acct


def add_txn(s, acct, amount, category, merchant="Shop", day=date(2026, 10, 10), removed=False, detailed=None):
    s.add(Transaction(user_id=acct.user_id, account_id=acct.id, plaid_txn_id=f"t-{acct.id}-{next(_txn_ids)}",
                      date=day, amount_cents=amount, merchant_name=merchant, pf_category_primary=category,
                      pf_category_detailed=detailed, removed=removed))
