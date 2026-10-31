import logging
from typing import Literal, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.budget_graph import resume_budget_run, waiting_for
from app.db.models import AdviceLog, AgentRun, Alert, PlaidItem

logger = logging.getLogger(__name__)

# the user's side of the budget agent (tpc-20): accept or dismiss an alert, write the outcome back to the advice
# it came from, and continue a run that was paused on it. shared by the api, and later the scheduler and close

# what tests/factories.add_user stores as an access token: seeded users have no real plaid item to sync
SEED_TOKEN = "x"

Decision = Literal["accepted", "dismissed"]


class AlertNotFound(Exception):
    pass


class AlertAlreadyDecided(Exception):
    pass


def has_real_plaid_item(session: Session, user_id: str) -> bool:
    return session.scalar(select(PlaidItem.id).where(PlaidItem.user_id == user_id,
                                                     PlaidItem.access_token_enc != SEED_TOKEN).limit(1)) is not None


# a checkpoint that can't be read (deleted file, older build) counts as not waiting: the alert stays decidable
def _waiting(session_factory, checkpointer: BaseCheckpointSaver, run_id: int) -> list[int]:
    try:
        return waiting_for(session_factory, checkpointer, run_id)
    except Exception:
        logger.warning("budget run %s checkpoint unreadable", run_id, exc_info=True)
        return []


# a paused run whose proposals were decided without a resume reaching it (the resume failed, or two decisions
# raced) is continued here. the run reads every decision from the alert rows, so one resume is enough
def _resume_decided(session_factory, checkpointer: BaseCheckpointSaver, run_id: int) -> None:
    waiting = _waiting(session_factory, checkpointer, run_id)
    if not waiting:
        return
    with session_factory() as s:
        decided = {a.id: a.status for a in s.scalars(select(Alert).where(
            Alert.id.in_(waiting), Alert.status.in_(("accepted", "dismissed"))))}
    if not decided:
        return
    alert_id = min(decided)
    try:
        resume_budget_run(session_factory, run_id, alert_id, decided[alert_id], checkpointer=checkpointer)
    except Exception:
        logger.warning("budget run %s resume failed", run_id, exc_info=True)


def _paused_runs(session_factory, user_id: str) -> list[int]:
    with session_factory() as s:
        return s.scalars(select(AgentRun.id).where(AgentRun.user_id == user_id, AgentRun.graph == "budget",
                                                   AgentRun.status == "awaiting_user")).all()


# loading the alerts also finishes any paused run whose decisions are already in
def awaiting_approval(session_factory, user_id: str, *, checkpointer: BaseCheckpointSaver) -> set[int]:
    for run_id in _paused_runs(session_factory, user_id):
        _resume_decided(session_factory, checkpointer, run_id)
    return {aid for run_id in _paused_runs(session_factory, user_id)
            for aid in _waiting(session_factory, checkpointer, run_id)}


# the decision is committed before any paused run is resumed, so a failing resume never loses the user's click.
# it's one conditional update: two requests for the same alert can't both pass a status check
def decide_alert(session_factory, user_id: str, alert_id: int, decision: Decision, *,
                 checkpointer: Optional[BaseCheckpointSaver] = None) -> Alert:
    with session_factory() as s:
        alert = s.get(Alert, alert_id)
        if alert is None or alert.user_id != user_id:
            raise AlertNotFound(alert_id)
        changed = s.execute(update(Alert).where(Alert.id == alert_id, Alert.status == "pending_user")
                            .values(status=decision)).rowcount
        if not changed:
            s.rollback()
            raise AlertAlreadyDecided(alert_id)
        for row in s.scalars(select(AdviceLog).where(AdviceLog.alert_id == alert_id)):
            row.outcome = decision
        s.commit()
        s.refresh(alert)
    if checkpointer is not None:
        for run_id in _paused_runs(session_factory, user_id):
            _resume_decided(session_factory, checkpointer, run_id)
    return alert
