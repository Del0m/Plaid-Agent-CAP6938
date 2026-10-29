from contextlib import asynccontextmanager
from fastapi import FastAPI, status
import logging
from . import config
from .agents.budget_graph import close_interrupted_runs
from .api import admin, alerts, budget, budget_hooks, chat, dashboard, plaid
from .scheduler import build_scheduler

logger = logging.getLogger(__name__)


# runs still marked running are from a server that stopped mid-run; mark them so the page stops waiting.
# then the agent's daily schedule, only when asked for (ENABLE_SCHEDULER), so dev servers and tests stay quiet
@asynccontextmanager
async def lifespan(_app: FastAPI):
    close_interrupted_runs(budget_hooks.session_factory)
    sched = None
    if config.ENABLE_SCHEDULER:
        sched = build_scheduler()
        sched.start()
        for job in sched.get_jobs():
            logger.info("scheduled %s, next run %s", job.id, job.next_run_time)
    try:
        yield
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


# Initialize the application (settings and .env are loaded in app/config.py)
app = FastAPI(lifespan=lifespan)
app.include_router(plaid.router)
app.include_router(dashboard.router)
app.include_router(chat.router)
app.include_router(alerts.router)
app.include_router(budget.router)
app.include_router(admin.router)

# Create an endpoint that ensures it is up and running
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok"}
