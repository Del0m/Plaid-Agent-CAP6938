from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from .agents.budget_graph import close_interrupted_runs
from .api import alerts, budget, budget_hooks, chat, dashboard, plaid


# budget runs still marked running are from a server that stopped mid-run; mark them so the page stops waiting
@asynccontextmanager
async def lifespan(_app: FastAPI):
    close_interrupted_runs(budget_hooks.session_factory)
    yield


# Initialize the application (settings and .env are loaded in app/config.py)
app = FastAPI(lifespan=lifespan)
app.include_router(plaid.router)
app.include_router(dashboard.router)
app.include_router(chat.router)
app.include_router(alerts.router)
app.include_router(budget.router)

# Create an endpoint that ensures it is up and running
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok"}
