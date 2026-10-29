import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import select

from app.budget_engine.txns import add_months
from app.budget_service.daily import close_month, run_daily_locked
from app.db.models import Account, AgentRun, Transaction, User

# simulated days for synthetic users (tpc-21): each advanced day injects that day's generated transactions,
# runs the month-end slot on the 1st, then the real budget run. the caller holds the user's run lock


class NotSimulated(Exception):
    pass


# the synthetic profiles live in eval/ at the repo root, outside the backend package; only the simulation needs
# them, so they're imported here, when a simulation actually runs
def _profiles():
    repo = str(Path(__file__).resolve().parents[3])
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from eval import budget_users
    return budget_users


# that day's generated transactions, skipping any already in (stable ids), so a retried day can't double up
def _inject(session_factory, user_id: str, profile, day: date) -> int:
    users = _profiles()
    todays = [(n, row) for n, row in enumerate(users.transactions(profile)) if row[0] == day]
    if not todays:
        return 0
    with session_factory() as s:
        account_id = s.scalar(select(Account.id).where(Account.user_id == user_id).order_by(Account.id).limit(1))
        have = set(s.scalars(select(Transaction.plaid_txn_id).where(
            Transaction.plaid_txn_id.in_([users.txn_id(profile, n) for n, _ in todays]))))
        added = 0
        for n, (when, cents, category, merchant) in todays:
            if users.txn_id(profile, n) in have:
                continue
            s.add(Transaction(user_id=user_id, account_id=account_id, plaid_txn_id=users.txn_id(profile, n),
                              date=when, amount_cents=cents, merchant_name=merchant, pf_category_primary=category))
            added += 1
        s.commit()
    return added


def _sim(session_factory, user_id: str) -> dict:
    with session_factory() as s:
        user = s.get(User, user_id)
        sim = (user.prefs_json or {}).get("sim") if user else None
    if not sim:
        raise NotSimulated(user_id)
    return sim


def _save_date(session_factory, user_id: str, day: date) -> None:
    with session_factory() as s:
        user = s.get(User, user_id)
        prefs = dict(user.prefs_json or {})
        prefs["sim"] = {**prefs["sim"], "date": day.isoformat()}
        user.prefs_json = prefs           # a new dict, so the json column sees the change
        s.commit()


def _save_progress(session_factory, job_id: int, progress: dict) -> None:
    with session_factory() as s:
        s.get(AgentRun, job_id).summary_json = dict(progress)
        s.commit()


def advance_days(session_factory, user_id: str, days: int, *, model: Optional[BaseChatModel],
                 checkpointer: Optional[BaseCheckpointSaver], job_id: Optional[int] = None,
                 on_day: Optional[Callable[[dict], None]] = None) -> dict:
    sim = _sim(session_factory, user_id)
    profile = next(p for p in _profiles().PROFILES + _profiles().PLANTED if p.id == sim["profile"])
    day = date.fromisoformat(sim["date"])
    progress = {"days_requested": days, "days_done": 0, "start_date": day.isoformat(), "date": day.isoformat(),
                "run_ids": [], "alerts_written": 0}
    for _ in range(days):
        day += timedelta(days=1)
        if day.day == 1:
            # the month that just ended closes before the new month's first run
            close_month(session_factory, user_id, add_months(day, -1))
        injected = _inject(session_factory, user_id, profile, day)
        state = run_daily_locked(session_factory, user_id, day, model=model, checkpointer=checkpointer)
        _save_date(session_factory, user_id, day)        # only after the run, so a failed day can be retried
        alerts = len(state.get("alert_ids") or [])
        progress.update(days_done=progress["days_done"] + 1, date=day.isoformat(),
                        run_ids=progress["run_ids"] + [state["run_id"]],
                        alerts_written=progress["alerts_written"] + alerts)
        if job_id is not None:
            _save_progress(session_factory, job_id, progress)
        if on_day is not None:
            on_day({"date": day.isoformat(), "injected": injected, "status": state["status"], "run_id": state["run_id"],
                    "flags": [f["category"] for f in state.get("flags") or []], "alerts": alerts})
    return progress
