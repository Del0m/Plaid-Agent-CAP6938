import importlib
import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from app import config
from app.budget_service import daily
from app.budget_service.daily import close_month, run_daily, run_daily_locked, sim_today, user_lock
from app.db.models import AgentRun, PlaidItem
from eval.budget_users import PLANTED, seed_profile
from tests.fakes import ScriptedChatModel

ONE = date(2026, 10, 20)


def seed(db, pid="overspend-one-time", until=None):
    with db() as s:
        return seed_profile(s, next(p for p in PLANTED if p.id == pid), until=until)


def model():
    return ScriptedChatModel(replies=[AIMessage(content=json.dumps({"cause": "one_time", "evidence": "One purchase."})),
                                      AIMessage(content=json.dumps({"action": "alert", "message": "Over."}))])


@pytest.fixture(autouse=True)
def audit_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")


def test_run_daily_none_when_locked(db):
    uid = seed(db)
    lock = user_lock(uid)
    lock.acquire()
    try:
        assert run_daily(db, uid, ONE, model=model(), checkpointer=None) is None
    finally:
        lock.release()
    with db() as s:
        assert s.query(AgentRun).count() == 0
    assert run_daily(db, uid, ONE, model=model(), checkpointer=None)["status"] == "ok"
    assert lock.acquire(blocking=False)
    lock.release()


def test_run_daily_locked_syncs_only_real_items(db, monkeypatch):
    uid, calls = seed(db), []
    monkeypatch.setattr(daily, "plaid_sync", lambda s, user_id: calls.append(user_id) or {"items": 1, "errors": []})
    run_daily_locked(db, uid, ONE, model=model(), checkpointer=None)
    assert calls == []
    with db() as s:
        s.query(PlaidItem).filter_by(user_id=uid).one().access_token_enc = "enc"
        s.commit()
    run_daily_locked(db, uid, ONE, model=ScriptedChatModel(replies=[
        AIMessage(content=json.dumps({"cause": "one_time", "evidence": "One purchase."})),
        AIMessage(content=json.dumps({"action": "no_op", "message": "Already told you."}))]), checkpointer=None)
    assert calls == [uid]


def test_close_month_placeholder_row(db):
    uid = seed(db)
    run_id = close_month(db, uid, date(2026, 10, 1))
    with db() as s:
        row = s.get(AgentRun, run_id)
        assert (row.graph, row.status, row.user_id) == ("close", "skipped", uid)
        assert row.summary_json == {"month": "2026-10-01", "note": "period close arrives with TPC-22"}
        assert row.finished_at is not None


def test_sim_today(db):
    sim = seed(db, until=date(2026, 9, 30))
    real = seed(db, "overspend-recurring")
    with db() as s:
        assert (sim_today(s, sim), sim_today(s, real)) == (date(2026, 9, 30), None)


@pytest.fixture
def reload_config(monkeypatch):
    yield lambda: importlib.reload(config)
    monkeypatch.undo()
    importlib.reload(config)


def test_config_flags(monkeypatch, reload_config):
    monkeypatch.setenv("ENABLE_SCHEDULER", "Yes")
    monkeypatch.setenv("ENABLE_ADMIN", "")
    reload_config()
    assert (config.ENABLE_SCHEDULER, config.ENABLE_ADMIN) == (True, False)
    assert (config.DAILY_RUN_TIME, config.CLOSE_RUN_TIME, config.SCHEDULER_TZ) == ("06:00", "05:00", "America/New_York")
