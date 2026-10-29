import calendar
import logging
import threading
from datetime import date as Date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..agents.budget_graph import plaid_sync, run_budget, waiting_for
from ..budget_engine.pace import BudgetLine as EngineLine, evaluate
from ..budget_service.alerts import has_real_plaid_item
from ..budget_service.initialize import load_txns
from ..db.models import AdviceLog, AgentRun, BudgetPeriod, User
from . import budget_hooks
from .deps import get_current_user

logger = logging.getLogger(__name__)

# the budget for the dashboard's pace bars, and running the budget agent from the ui (tpc-20). a run takes
# tens of seconds and the next.js proxy drops a request after 30 s of silence, so it runs in a background
# thread and the page polls GET /budget/runs/{id}

router = APIRouter(tags=["budget"])
START_WAIT_SECONDS = 5.0

# one run in flight per user, like chat: a run holds the local model. in-process, so one uvicorn worker
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _user_lock(user_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(user_id, threading.Lock())


def _today() -> Date:
    return Date.today()


# how a run is started; tests run it inline, since their in-memory db is one connection threads can't share
def _spawn(work) -> None:
    threading.Thread(target=work, daemon=True).start()


class PeriodOut(BaseModel):
    start: Date
    end: Date

class LineOut(BaseModel):
    category: str
    kind: str
    limit_cents: int
    spent_cents: int
    pace: Optional[float]
    flagged: bool
    reason: Optional[str]

class UnbudgetedOut(BaseModel):
    category: str
    spent_cents: int

class BudgetOut(BaseModel):
    period: Optional[PeriodOut]
    days_elapsed: int
    days_in_period: int
    lines: list[LineOut]
    unbudgeted: list[UnbudgetedOut]

class RunRequest(BaseModel):
    date: Optional[Date] = None

class RunOut(BaseModel):
    run_id: int
    status: str
    alert_ids: list[int]
    waiting_for: list[int]
    error: Optional[str]


# live pace, nothing written: the bars are current right after a sync, without waiting for the daily run
@router.get("/budget", response_model=BudgetOut)
def get_budget(user: User = Depends(get_current_user)):
    today = _today()
    days = calendar.monthrange(today.year, today.month)[1]
    with budget_hooks.session_factory() as s:
        period = s.scalars(select(BudgetPeriod).where(BudgetPeriod.user_id == user.id)
                           .order_by(BudgetPeriod.start_date.desc()).limit(1)).first()
        if period is None or not (period.start_date <= today <= period.end_date):
            return BudgetOut(period=None, days_elapsed=today.day, days_in_period=days, lines=[], unbudgeted=[])
        results = evaluate([EngineLine(ln.category, ln.kind, ln.limit_cents) for ln in period.lines],
                           load_txns(s, user.id, period.start_date, today), today)
        out = PeriodOut(start=period.start_date, end=period.end_date)
    return BudgetOut(
        period=out, days_elapsed=today.day, days_in_period=days,
        lines=[LineOut(category=r.category, kind=r.kind, limit_cents=r.limit_cents, spent_cents=r.spent_cents,
                       pace=r.pace, flagged=r.flagged, reason=r.reason) for r in results if r.kind != "unbudgeted"],
        unbudgeted=[UnbudgetedOut(category=r.category, spent_cents=r.spent_cents)
                    for r in results if r.kind == "unbudgeted"])


@router.post("/budget/run", status_code=202)
def start_run(req: RunRequest, user: User = Depends(get_current_user)):
    lock = _user_lock(user.id)
    if not lock.acquire(blocking=False):
        return JSONResponse({"detail": "A budget run is already in progress."}, status_code=409)
    factory = budget_hooks.session_factory
    with factory() as s:
        sync_fn = plaid_sync if has_real_plaid_item(s, user.id) else None   # seeded users have nothing to sync
    started, run_ids = threading.Event(), []

    def on_start(run_id: int) -> None:
        run_ids.append(run_id)
        started.set()

    def work() -> None:
        try:
            run_budget(factory, user.id, req.date or _today(), model=budget_hooks.make_model(), sync_fn=sync_fn,
                       checkpointer=budget_hooks.get_checkpointer(), on_start=on_start)
        except Exception:
            logger.warning("budget run for a user failed", exc_info=True)   # the run row says error
        finally:
            started.set()
            lock.release()

    _spawn(work)
    started.wait(START_WAIT_SECONDS)
    return {"run_id": run_ids[0] if run_ids else None}


@router.get("/budget/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, user: User = Depends(get_current_user)):
    with budget_hooks.session_factory() as s:
        run = s.get(AgentRun, run_id)
        if run is None or run.user_id != user.id or run.graph != "budget":
            raise HTTPException(404, "Run not found.")
        status, summary = run.status, run.summary_json or {}
        alert_ids = sorted(a for a in s.scalars(select(AdviceLog.alert_id).where(AdviceLog.run_id == run_id))
                           if a is not None)
    waiting = waiting_for(budget_hooks.session_factory, budget_hooks.get_checkpointer(), run_id) \
        if status == "awaiting_user" else []
    return RunOut(run_id=run_id, status=status, alert_ids=alert_ids, waiting_for=waiting,
                  error=summary.get("error") if status == "error" else None)
