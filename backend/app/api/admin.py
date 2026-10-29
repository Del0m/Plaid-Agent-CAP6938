import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import config
from ..budget_service.daily import sim_today, user_lock
from ..budget_service.simulate import advance_days
from ..db.models import AgentRun, User, utcnow
from . import budget as budget_routes
from . import budget_hooks
from .deps import get_current_user

logger = logging.getLogger(__name__)

# development tools (tpc-21): advance a synthetic user's simulated date, a day of generated transactions and a
# real budget run at a time. off unless ENABLE_ADMIN is set; real users can never be advanced


def require_admin() -> None:
    if not config.ENABLE_ADMIN:
        raise HTTPException(404, "Not found.")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class SimStateOut(BaseModel):
    simulated: bool
    profile: Optional[str]
    date: Optional[str]

class AdvanceRequest(BaseModel):
    days: int = Field(ge=1, le=31)

class JobOut(BaseModel):
    job_id: int
    status: str
    days_requested: int
    days_done: int
    start_date: Optional[str]
    date: Optional[str]
    run_ids: list[int]
    alerts_written: int
    error: Optional[str]


@router.get("/sandbox", response_model=SimStateOut)
def sandbox_state(user: User = Depends(get_current_user)):
    sim = (user.prefs_json or {}).get("sim")
    return SimStateOut(simulated=bool(sim), profile=sim and sim["profile"], date=sim and sim["date"])


@router.post("/sandbox/advance", status_code=202)
def advance(req: AdvanceRequest, user: User = Depends(get_current_user)):
    lock = user_lock(user.id)        # shared with the run button and the scheduler
    if not lock.acquire(blocking=False):
        return JSONResponse({"detail": "A budget run is already in progress."}, status_code=409)
    factory = budget_hooks.session_factory
    try:
        with factory() as s:
            start = sim_today(s, user.id)
            if start is None:
                lock.release()
                return JSONResponse({"detail": "Only simulated users can be advanced."}, status_code=409)
            job = AgentRun(user_id=user.id, graph="simulate", status="running", summary_json={
                "days_requested": req.days, "days_done": 0, "start_date": start.isoformat(),
                "date": start.isoformat(), "run_ids": [], "alerts_written": 0})
            s.add(job)
            s.commit()
            job_id = job.id

        def work() -> None:
            status, error = "ok", None
            try:
                advance_days(factory, user.id, req.days, model=budget_hooks.make_model(),
                             checkpointer=budget_hooks.get_checkpointer(), job_id=job_id)
            except Exception as exc:
                logger.warning("simulation job %s failed", job_id, exc_info=True)
                status, error = "error", type(exc).__name__
            finally:
                # the lock is released whatever happens to the status write: a write failing here (sqlite
                # "database is locked") must not lock the user out of every run until a restart
                try:
                    with factory() as s:
                        row = s.get(AgentRun, job_id)
                        row.status, row.finished_at = status, utcnow()
                        if error:
                            row.summary_json = {**(row.summary_json or {}), "error": error}
                        s.commit()
                except Exception:
                    logger.warning("simulation job %s: couldn't record its status", job_id, exc_info=True)
                finally:
                    lock.release()

        budget_routes._spawn(work)
    except Exception:
        # until the thread owns the lock, this request does: hand it back, or every later run gets 409
        if lock.locked():
            lock.release()
        logger.warning("simulation couldn't start", exc_info=True)
        return JSONResponse({"detail": "The simulation couldn't start."}, status_code=500)
    return {"job_id": job_id}


@router.get("/sandbox/advance/{job_id}", response_model=JobOut)
def advance_status(job_id: int, user: User = Depends(get_current_user)):
    with budget_hooks.session_factory() as s:
        row = s.get(AgentRun, job_id)
        if row is None or row.user_id != user.id or row.graph != "simulate":
            raise HTTPException(404, "Not found.")
        summary = row.summary_json or {}
        return JobOut(job_id=job_id, status=row.status, days_requested=summary.get("days_requested", 0),
                      days_done=summary.get("days_done", 0), start_date=summary.get("start_date"),
                      date=summary.get("date"), run_ids=summary.get("run_ids", []),
                      alerts_written=summary.get("alerts_written", 0), error=summary.get("error"))
