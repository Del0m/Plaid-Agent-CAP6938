from datetime import date as Date
from typing import Literal, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from ..db.models import Account, Liability, PlaidItem, User
from ..db.session import get_session
from ..finance import avalanche_key
from .deps import get_current_user

# what the dashboard page shows. reads our db only, never plaid, so it's fast and safe to reload

router = APIRouter(tags=["dashboard"])

# plaid reports both sides as positive amounts: money held vs money owed
ASSET_TYPES = {"depository", "investment", "brokerage"}
DEBT_TYPES = {"credit", "loan"}


class AccountOut(BaseModel):
    id: int
    name: str
    mask: Optional[str]
    type: str
    group: Literal["asset", "debt", "other"]
    subtype: Optional[str]
    current_balance_cents: Optional[int]
    available_balance_cents: Optional[int]
    institution_name: Optional[str]

class DebtOut(BaseModel):
    account_id: int
    name: str
    mask: Optional[str]
    kind: str
    apr: Optional[float]
    balance_cents: Optional[int]
    min_payment_cents: Optional[int]
    next_due_date: Optional[Date]

class UnsyncedItemOut(BaseModel):
    id: int
    institution_name: Optional[str]
    syncing: bool

class DashboardResponse(BaseModel):
    net_worth_cents: int
    assets_cents: int
    debts_cents: int
    accounts_missing_balance: int        # asset/debt accounts left out of the totals for lack of a balance
    accounts: list[AccountOut]
    debts: list[DebtOut]
    unsynced_items: list[UnsyncedItemOut]  # linked, but the first sync never finished; the page offers a retry


def _group(account_type: str) -> int:
    return 0 if account_type in ASSET_TYPES else 1 if account_type in DEBT_TYPES else 2


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    rows = session.execute(
        select(Account, PlaidItem.institution_name)
        .join(PlaidItem, Account.item_id == PlaidItem.id)
        .where(Account.user_id == user.id)
    ).all()

    assets = debts = missing = 0
    for acct, _ in rows:
        group = _group(acct.type)
        if group == 2:
            continue
        if acct.current_balance_cents is None:
            missing += 1
        elif group == 0:
            assets += acct.current_balance_cents
        else:
            debts += acct.current_balance_cents

    accounts = [
        AccountOut(id=a.id, name=a.name, mask=a.mask, type=a.type, group=("asset", "debt", "other")[_group(a.type)], subtype=a.subtype,
                   current_balance_cents=a.current_balance_cents,
                   available_balance_cents=a.available_balance_cents, institution_name=inst)
        for a, inst in sorted(rows, key=lambda r: (_group(r[0].type), r[0].name.casefold(), r[0].id))
    ]

    liabs = session.execute(
        select(Liability, Account)
        .join(Account, Liability.account_id == Account.id)
        .where(Liability.user_id == user.id)
    ).all()
    debt_rows = [
        DebtOut(account_id=a.id, name=a.name, mask=a.mask, kind=l.kind, apr=l.apr,
                balance_cents=a.current_balance_cents, min_payment_cents=l.min_payment_cents,
                next_due_date=l.next_due_date)
        for l, a in liabs
    ]
    # avalanche order: highest APR first, unknown APR last, bigger balance first on a tie
    debt_rows.sort(key=lambda d: avalanche_key(d.apr, d.balance_cents))

    # A cursor only proves transactions succeeded. Later balance/liability failures stay retryable.
    unsynced = session.scalars(
        select(PlaidItem).where(PlaidItem.user_id == user.id,
                               or_(PlaidItem.txn_cursor.is_(None),
                                   PlaidItem.status.in_(("syncing", "sync_failed")))).order_by(PlaidItem.id)
    ).all()

    return DashboardResponse(
        net_worth_cents=assets - debts, assets_cents=assets, debts_cents=debts,
        accounts_missing_balance=missing, accounts=accounts, debts=debt_rows,
        unsynced_items=[UnsyncedItemOut(id=i.id, institution_name=i.institution_name,
                                      syncing=i.status == "syncing") for i in unsynced],
    )
