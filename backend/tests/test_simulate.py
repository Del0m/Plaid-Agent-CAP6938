from datetime import date, timedelta

import pytest

from app import config
from app.budget_service import simulate
from app.budget_service.simulate import NotSimulated, advance_days
from app.db.models import AgentRun, Transaction, User
from eval.budget_users import PLANTED, seed_profile, transactions
from tests.fakes import SchemaReplyModel


@pytest.fixture(autouse=True)
def audit_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")


def profile(pid):
    return next(p for p in PLANTED if p.id == pid)


def seed(db, pid="overspend-one-time", until=date(2026, 9, 30)):
    with db() as s:
        return seed_profile(s, profile(pid), until=until)


def advance(db, uid, days, **kw):
    days_seen = []
    result = advance_days(db, uid, days, model=SchemaReplyModel(), checkpointer=None, on_day=days_seen.append, **kw)
    return result, days_seen


def sim_date(db, uid):
    with db() as s:
        return s.get(User, uid).prefs_json["sim"]["date"]


def runs(db, graph):
    with db() as s:
        return s.query(AgentRun).filter_by(graph=graph).order_by(AgentRun.id).all()


def test_thirty_days_thirty_runs(db):
    uid = seed(db)
    result, days = advance(db, uid, 30)
    assert [d["date"] for d in days] == [(date(2026, 10, 1) + timedelta(n)).isoformat() for n in range(30)]
    assert len(runs(db, "budget")) == 30 and len(result["run_ids"]) == 30
    assert (result["days_requested"], result["days_done"], result["start_date"], result["date"]) == (
        30, 30, "2026-09-30", "2026-10-30")
    assert sim_date(db, uid) == "2026-10-30"


def test_each_day_injects_its_transactions(db):
    uid = seed(db)
    _, days = advance(db, uid, 12)
    per_day = {}
    for d, *_ in transactions(profile("overspend-one-time")):
        per_day[d.isoformat()] = per_day.get(d.isoformat(), 0) + 1
    assert [d["injected"] for d in days] == [per_day.get(d["date"], 0) for d in days]
    with db() as s:
        assert s.query(Transaction).filter_by(user_id=uid, merchant_name="ElectroMart").count() == 1
        assert max(t.date for t in s.query(Transaction).filter_by(user_id=uid)) == date(2026, 10, 12)


def test_month_boundary_closes_first(db):
    uid = seed(db, until=date(2026, 10, 30))
    _, days = advance(db, uid, 3)
    [close] = runs(db, "close")
    nov1 = next(d for d in days if d["date"] == "2026-11-01")
    assert close.summary_json["month"] == "2026-10-01" and close.id < nov1["run_id"]


def test_close_across_new_year(db):
    uid = seed(db, "overspend-seasonal", until=date(2026, 12, 30))
    _, days = advance(db, uid, 3)
    [close] = runs(db, "close")
    jan1 = next(d for d in days if d["date"] == "2027-01-01")
    assert close.summary_json["month"] == "2026-12-01" and close.id < jan1["run_id"]


def test_failed_day_stops_and_resumes(db, monkeypatch):
    uid = seed(db)
    real, calls = simulate.run_daily_locked, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("model down")
        return real(*a, **k)

    monkeypatch.setattr(simulate, "run_daily_locked", flaky)
    with pytest.raises(RuntimeError):
        advance(db, uid, 5)
    assert sim_date(db, uid) == "2026-10-02"
    monkeypatch.setattr(simulate, "run_daily_locked", real)
    _, days = advance(db, uid, 1)
    assert [d["date"] for d in days] == ["2026-10-03"]


def test_runs_past_profile_end(db):
    uid = seed(db, until=date(2026, 10, 31))
    _, days = advance(db, uid, 2)
    assert [(d["date"], d["injected"]) for d in days] == [("2026-11-01", 0), ("2026-11-02", 0)]
    assert len(runs(db, "budget")) == 2


def test_not_simulated_raises(db):
    with db() as s:
        uid = seed_profile(s, profile("overspend-one-time"))
    with pytest.raises(NotSimulated):
        advance(db, uid, 1)


def test_reinject_is_idempotent(db):
    uid = seed(db)
    p = profile("overspend-one-time")
    assert simulate._inject(db, uid, p, date(2026, 10, 12)) >= 1
    with db() as s:
        before = s.query(Transaction).filter_by(user_id=uid).count()
    assert simulate._inject(db, uid, p, date(2026, 10, 12)) == 0
    with db() as s:
        assert s.query(Transaction).filter_by(user_id=uid).count() == before


def test_job_summary_updates(db):
    uid = seed(db)
    with db() as s:
        job = AgentRun(user_id=uid, graph="simulate", status="running", summary_json={})
        s.add(job)
        s.commit()
        job_id = job.id
    advance(db, uid, 3, job_id=job_id)
    with db() as s:
        summary = s.get(AgentRun, job_id).summary_json
    assert (summary["days_done"], summary["date"], len(summary["run_ids"])) == (3, "2026-10-03", 3)
