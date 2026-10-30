from datetime import date as Date, datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..budget_service.alerts import AlertAlreadyDecided, AlertNotFound, awaiting_approval, decide_alert
from ..db.models import Alert, BudgetPeriod, User
from . import budget_hooks
from .deps import get_current_user

# the agent's alerts and the user's answer to them (tpc-20). accepting or dismissing a budget proposal also
# continues the run that was paused waiting for it

router = APIRouter(tags=["alerts"])


class AlertOut(BaseModel):
    id: int
    kind: str
    category: Optional[str]
    severity: str
    cause: Optional[str]
    action: Optional[str]
    message: str
    proposed_limit_cents: Optional[int]
    status: str
    created_at: datetime
    period_start: Optional[Date]
    awaiting_approval: bool        # a paused budget run is waiting on this decision


def _out(alert: Alert, period_start: Optional[Date], waiting: set[int]) -> AlertOut:
    return AlertOut(id=alert.id, kind=alert.kind, category=alert.category, severity=alert.severity, cause=alert.cause,
                    action=alert.action, message=alert.message, proposed_limit_cents=alert.proposed_limit_cents,
                    status=alert.status, created_at=alert.created_at, period_start=period_start,
                    awaiting_approval=alert.id in waiting)


def _load(user_id: str, alert_id: Optional[int] = None,
          status: Literal["pending_user", "all"] = "all") -> list[AlertOut]:
    query = (select(Alert, BudgetPeriod.start_date).outerjoin(BudgetPeriod, Alert.period_id == BudgetPeriod.id)
             .where(Alert.user_id == user_id).order_by(Alert.created_at.desc(), Alert.id.desc()))
    if alert_id is not None:
        query = query.where(Alert.id == alert_id)
    if status == "pending_user":
        query = query.where(Alert.status == "pending_user")
    with budget_hooks.session_factory() as s:
        rows = s.execute(query).all()
    waiting = awaiting_approval(budget_hooks.session_factory, user_id, checkpointer=budget_hooks.get_checkpointer())
    return [_out(a, start, waiting) for a, start in rows]


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(status: Literal["pending_user", "all"] = "all", user: User = Depends(get_current_user)):
    return _load(user.id, status=status)


def _decide(alert_id: int, decision: Literal["accepted", "dismissed"], user: User) -> AlertOut:
    try:
        decide_alert(budget_hooks.session_factory, user.id, alert_id, decision,
                     checkpointer=budget_hooks.get_checkpointer())
    except AlertNotFound:
        raise HTTPException(404, "Alert not found.")
    except AlertAlreadyDecided:
        raise HTTPException(409, "This alert was already decided.")
    return _load(user.id, alert_id=alert_id)[0]


@router.post("/alerts/{alert_id}/accept", response_model=AlertOut)
def accept_alert(alert_id: int, user: User = Depends(get_current_user)):
    return _decide(alert_id, "accepted", user)


@router.post("/alerts/{alert_id}/dismiss", response_model=AlertOut)
def dismiss_alert(alert_id: int, user: User = Depends(get_current_user)):
    return _decide(alert_id, "dismissed", user)
