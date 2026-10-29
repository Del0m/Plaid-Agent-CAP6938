import logging
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import config
from .api import budget_hooks
from .budget_engine.txns import add_months, month_start
from .budget_service.alerts import SEED_TOKEN
from .budget_service.daily import close_month, run_daily
from .db.models import AgentRun, BudgetPeriod, PlaidItem, User

logger = logging.getLogger(__name__)

# the agent's own schedule (tpc-21): every morning a budget check for each real user, and on the 1st the
# month-end slot before it. jobs are plain functions so tests call them directly instead of waiting for a clock


def _today() -> date:
    return datetime.now(ZoneInfo(config.SCHEDULER_TZ)).date()


# users with a budget or a real plaid item. simulated users live on their own date and are advanced by hand
def eligible_users(session: Session) -> list[str]:
    has_budget = select(BudgetPeriod.user_id)
    has_item = select(PlaidItem.user_id).where(PlaidItem.access_token_enc != SEED_TOKEN)
    users = session.scalars(select(User).where(or_(User.id.in_(has_budget), User.id.in_(has_item)))).all()
    return sorted(u.id for u in users if not (u.prefs_json or {}).get("sim"))


# one user at a time (there's one local model). a user already running (the button, a simulation) is skipped
# today; one user's failure is on their run row and doesn't stop the rest
def _ran_today(session, user_id: str, today: date) -> bool:
    started = session.scalars(select(AgentRun.started_at).where(AgentRun.user_id == user_id,
                                                                AgentRun.graph == "budget")).all()
    tz = ZoneInfo(config.SCHEDULER_TZ)
    return any(t.astimezone(tz).date() == today for t in started)


# skip_done (the startup catch-up): leave out users whose budget already ran today, by any route
def daily_budget_job(session_factory, *, today: Optional[date] = None, model=None, checkpointer=None,
                     skip_done: bool = False) -> dict:
    today = today or _today()
    with session_factory() as s:
        user_ids = eligible_users(s)
        done = {u for u in user_ids if skip_done and _ran_today(s, u, today)}
    result = {"ran": [], "skipped": sorted(done), "failed": []}
    for user_id in user_ids:
        if user_id in done:
            continue
        try:
            state = run_daily(session_factory, user_id, today, model=model or budget_hooks.make_model(),
                              checkpointer=checkpointer or budget_hooks.get_checkpointer())
        except Exception:
            logger.warning("scheduled budget run failed for one user", exc_info=True)
            result["failed"].append(user_id)
            continue
        result["skipped" if state is None else "ran"].append(user_id)
    logger.info("daily budget job: %d ran, %d skipped, %d failed",
                len(result["ran"]), len(result["skipped"]), len(result["failed"]))
    return result


# skip_done (the startup catch-up): leave out users whose close for that month is already recorded
def month_end_job(session_factory, *, today: Optional[date] = None, skip_done: bool = False) -> dict:
    previous = add_months(month_start(today or _today()), -1)
    with session_factory() as s:
        user_ids = eligible_users(s)
        if skip_done:
            closed = {r.user_id for r in s.scalars(select(AgentRun).where(AgentRun.graph == "close"))
                      if (r.summary_json or {}).get("month") == previous.isoformat()}
            user_ids = [u for u in user_ids if u not in closed]
    for user_id in user_ids:
        close_month(session_factory, user_id, previous)
    return {"closed": user_ids}


def _at(hhmm: str) -> dict:
    hour, minute = hhmm.split(":")
    return {"hour": int(hour), "minute": int(minute)}


# not started here: the app's startup starts it when ENABLE_SCHEDULER is on. the cron slots only fire while the
# server is up (the misfire grace covers a busy server, not one that was down); add_catch_up_jobs covers today
def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=config.SCHEDULER_TZ)
    options = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
    sched.add_job(daily_budget_job, CronTrigger(**_at(config.DAILY_RUN_TIME), timezone=config.SCHEDULER_TZ),
                  args=[budget_hooks.session_factory], id="daily_budget", **options)
    sched.add_job(month_end_job, CronTrigger(day=1, **_at(config.CLOSE_RUN_TIME), timezone=config.SCHEDULER_TZ),
                  args=[budget_hooks.session_factory], id="month_end", **options)
    return sched


# a server started after today's slot would otherwise wait for tomorrow (the in-memory job store forgets missed
# fires). run today's slot once, now, for users it hasn't covered yet. earlier days are never replayed
def add_catch_up_jobs(sched: BackgroundScheduler, now: datetime) -> list[str]:
    now = now.astimezone(ZoneInfo(config.SCHEDULER_TZ))
    added = []
    slots = [("catch_up_daily", daily_budget_job, config.DAILY_RUN_TIME)]
    if now.day == 1:
        slots.append(("catch_up_month_end", month_end_job, config.CLOSE_RUN_TIME))
    for job_id, job, hhmm in slots:
        at = _at(hhmm)
        if (now.hour, now.minute) >= (at["hour"], at["minute"]):
            sched.add_job(job, DateTrigger(run_date=now), args=[budget_hooks.session_factory],
                          kwargs={"today": now.date(), "skip_done": True}, id=job_id, misfire_grace_time=3600)
            added.append(job_id)
    return added
