import logging
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import config
from .api import budget_hooks
from .budget_engine.txns import add_months, month_start
from .budget_service.alerts import SEED_TOKEN
from .budget_service.daily import close_month, run_daily
from .db.models import BudgetPeriod, PlaidItem, User

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
def daily_budget_job(session_factory, *, today: Optional[date] = None, model=None, checkpointer=None) -> dict:
    today = today or _today()
    with session_factory() as s:
        user_ids = eligible_users(s)
    result = {"ran": [], "skipped": [], "failed": []}
    for user_id in user_ids:
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


def month_end_job(session_factory, *, today: Optional[date] = None) -> dict:
    previous = add_months(month_start(today or _today()), -1)
    with session_factory() as s:
        user_ids = eligible_users(s)
    for user_id in user_ids:
        close_month(session_factory, user_id, previous)
    return {"closed": user_ids}


def _at(hhmm: str) -> dict:
    hour, minute = hhmm.split(":")
    return {"hour": int(hour), "minute": int(minute)}


# not started here: the app's startup starts it when ENABLE_SCHEDULER is on. a run missed while the server was
# down still happens within the hour, once; later than that it waits for the next day (pace is about today)
def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=config.SCHEDULER_TZ)
    options = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
    sched.add_job(daily_budget_job, CronTrigger(**_at(config.DAILY_RUN_TIME), timezone=config.SCHEDULER_TZ),
                  args=[budget_hooks.session_factory], id="daily_budget", **options)
    sched.add_job(month_end_job, CronTrigger(day=1, **_at(config.CLOSE_RUN_TIME), timezone=config.SCHEDULER_TZ),
                  args=[budget_hooks.session_factory], id="month_end", **options)
    return sched
