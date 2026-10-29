import logging
from typing import Literal, Optional

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.budget_graph import resume_budget_run, waiting_for
from app.agents.llm import get_chat_model
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


def awaiting_approval(session_factory, user_id: str, *, checkpointer: BaseCheckpointSaver) -> set[int]:
    with session_factory() as s:
        run_ids = s.scalars(select(AgentRun.id).where(AgentRun.user_id == user_id, AgentRun.graph == "budget",
                                                      AgentRun.status == "awaiting_user")).all()
    return {aid for run_id in run_ids for aid in _waiting(session_factory, checkpointer, run_id)}


# the decision is committed before the paused run is resumed, so a failing resume never loses the user's click
def decide_alert(session_factory, user_id: str, alert_id: int, decision: Decision, *,
                 checkpointer: Optional[BaseCheckpointSaver] = None,
                 model: Optional[BaseChatModel] = None) -> Alert:
    with session_factory() as s:
        alert = s.get(Alert, alert_id)
        if alert is None or alert.user_id != user_id:
            raise AlertNotFound(alert_id)
        if alert.status != "pending_user":
            raise AlertAlreadyDecided(alert_id)
        alert.status = decision
        advice = s.scalars(select(AdviceLog).where(AdviceLog.alert_id == alert_id)).all()
        for row in advice:
            row.outcome = decision
        s.commit()
        run_ids = {row.run_id for row in advice if row.run_id is not None}
        paused = s.scalars(select(AgentRun.id).where(AgentRun.id.in_(run_ids),
                                                     AgentRun.status == "awaiting_user")).all()
    if checkpointer is not None:
        for run_id in paused:
            if alert_id not in _waiting(session_factory, checkpointer, run_id):
                continue
            try:
                resume_budget_run(session_factory, run_id, alert_id, decision, checkpointer=checkpointer,
                                  model=model or get_chat_model())
            except Exception:
                logger.warning("budget run %s resume failed", run_id, exc_info=True)
    return alert
