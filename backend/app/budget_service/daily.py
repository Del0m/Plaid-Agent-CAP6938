import threading
from datetime import date
from typing import Optional

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.orm import Session

from app.agents.budget_graph import plaid_sync, run_budget
from app.budget_service.alerts import has_real_plaid_item
from app.db.models import AgentRun, User, utcnow

# one budget run per user per day, whoever starts it (tpc-21): the scheduler, the run button, or a simulated day.
# they share one lock per user, so they can never overlap; in-process, so the app runs one uvicorn worker

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def user_lock(user_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(user_id, threading.Lock())


# a simulated user's "today" (prefs_json["sim"]["date"]); None for everyone living on the real date
def sim_today(session: Session, user_id: str) -> Optional[date]:
    user = session.get(User, user_id)
    sim = (user.prefs_json or {}).get("sim") if user else None
    return date.fromisoformat(sim["date"]) if sim else None


# the caller holds the user's lock. users without a real plaid item (seeded ones) have nothing to sync
def run_daily_locked(session_factory, user_id: str, today: date, *, model: Optional[BaseChatModel],
                     checkpointer: Optional[BaseCheckpointSaver], on_start=None) -> dict:
    with session_factory() as s:
        sync_fn = plaid_sync if has_real_plaid_item(s, user_id) else None
    return run_budget(session_factory, user_id, today, model=model, sync_fn=sync_fn, checkpointer=checkpointer,
                      on_start=on_start)


# None when another run for this user is going; the caller decides whether that's a skip or a 409
def run_daily(session_factory, user_id: str, today: date, *, model: Optional[BaseChatModel],
              checkpointer: Optional[BaseCheckpointSaver]) -> Optional[dict]:
    lock = user_lock(user_id)
    if not lock.acquire(blocking=False):
        return None
    try:
        return run_daily_locked(session_factory, user_id, today, model=model, checkpointer=checkpointer)
    finally:
        lock.release()


# the month-end slot. period close is tpc-22; until then the slot only records that it ran
def close_month(session_factory, user_id: str, month_start: date) -> int:
    with session_factory() as s:
        row = AgentRun(user_id=user_id, graph="close", status="skipped", finished_at=utcnow(),
                       summary_json={"month": month_start.isoformat(), "note": "period close arrives with TPC-22"})
        s.add(row)
        s.commit()
        return row.id
