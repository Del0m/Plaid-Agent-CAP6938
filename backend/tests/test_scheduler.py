from datetime import date

import pytest

from app import config, scheduler
from app.agents.budget_graph import run_budget
from app.budget_service.daily import user_lock
from app.db.models import AgentRun, PlaidItem
from app.scheduler import build_scheduler, daily_budget_job, eligible_users, month_end_job
from eval.budget_users import PLANTED, seed_profile
from tests.fakes import SchemaReplyModel

ONE = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def audit_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")


def planted(pid):
    return next(p for p in PLANTED if p.id == pid)


# three users the job should run (one with a budget, one with a real item, one with both),
# one simulated user and one with nothing: neither is the scheduler's business
@pytest.fixture
def users(db):
    with db() as s:
        budgeted = seed_profile(s, planted("overspend-one-time"))
        real = seed_profile(s, planted("overspend-recurring"))
        both = seed_profile(s, planted("overspend-known-merchant"))
        simulated = seed_profile(s, planted("overspend-seasonal"), until=date(2026, 11, 30))
        for uid in (real, both):
            s.query(PlaidItem).filter_by(user_id=uid).one().access_token_enc = "enc"
        s.commit()
    for uid in (budgeted, both):
        run_budget(db, uid, date(2026, 10, 2), model=SchemaReplyModel())
    run_budget(db, simulated, date(2026, 12, 2), model=SchemaReplyModel())
    return {"budgeted": budgeted, "real": real, "both": both, "simulated": simulated}


def test_eligible_users(db, users):
    with db() as s:
        assert eligible_users(s) == sorted([users["budgeted"], users["real"], users["both"]])


def test_daily_job_runs_eligible(db, users, monkeypatch):
    monkeypatch.setattr("app.budget_service.daily.plaid_sync", lambda s, uid: {"items": 1, "errors": []})
    result = daily_budget_job(db, today=ONE, model=SchemaReplyModel(), checkpointer=None)
    assert result == {"ran": sorted([users["budgeted"], users["real"], users["both"]]), "skipped": [], "failed": []}
    with db() as s:
        assert s.query(AgentRun).filter_by(user_id=users["simulated"]).count() == 1      # only its own seed run


def test_daily_job_skips_locked_and_survives_failure(db, users, monkeypatch):
    real_run = scheduler.run_daily

    def flaky(session_factory, user_id, today, **kw):
        if user_id == users["real"]:
            raise RuntimeError("plaid down")
        return real_run(session_factory, user_id, today, **kw)

    monkeypatch.setattr(scheduler, "run_daily", flaky)
    lock = user_lock(users["budgeted"])
    lock.acquire()
    try:
        result = daily_budget_job(db, today=ONE, model=SchemaReplyModel(), checkpointer=None)
    finally:
        lock.release()
    assert result == {"ran": [users["both"]], "skipped": [users["budgeted"]], "failed": [users["real"]]}


def test_month_end_job_closes_previous_month(db, users):
    assert month_end_job(db, today=date(2026, 11, 1)) == {
        "closed": sorted([users["budgeted"], users["real"], users["both"]])}
    with db() as s:
        rows = s.query(AgentRun).filter_by(graph="close").all()
    assert len(rows) == 3 and {r.summary_json["month"] for r in rows} == {"2026-10-01"}


def test_build_scheduler_jobs():
    sched = build_scheduler()
    jobs = {job.id: job for job in sched.get_jobs()}
    assert set(jobs) == {"daily_budget", "month_end"}
    fields = {jid: {f.name: str(f) for f in job.trigger.fields if not f.is_default} for jid, job in jobs.items()}
    assert fields["daily_budget"] == {"hour": "6", "minute": "0"}
    assert fields["month_end"] == {"day": "1", "hour": "5", "minute": "0"}
    for job in jobs.values():
        assert str(job.trigger.timezone) == "America/New_York"
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (1, True, 3600)
    assert not sched.running


class Recorder:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def shutdown(self, wait=True):
        self.calls.append("shutdown")

    def get_jobs(self):
        return []


def test_lifespan_starts_only_when_enabled(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.api import budget_hooks
    monkeypatch.setattr(budget_hooks, "session_factory", db)
    built = []
    monkeypatch.setattr(main, "build_scheduler", lambda: built.append(Recorder()) or built[-1])
    monkeypatch.setattr(config, "ENABLE_SCHEDULER", False)
    with TestClient(main.app):
        pass
    assert built == []
    monkeypatch.setattr(config, "ENABLE_SCHEDULER", True)
    with TestClient(main.app):
        pass
    assert built[0].calls == ["start", "shutdown"]
