import json
from datetime import date
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.agents.budget_graph import run_budget
from app.api import budget_hooks
from app.budget_service.alerts import decide_alert
from app.db.models import Alert, User
from eval.budget_users import PLANTED, seed_profile
from tests.fakes import ScriptedChatModel

ONE = date(2026, 10, 20)


def reply(**kw):
    return AIMessage(content=json.dumps(kw))


DIAG = reply(cause="one_time", evidence="A $600 purchase.")
ALERT = reply(action="alert", message="A $600 purchase put you over.")
RECUR = reply(cause="recurring", evidence="A new gym.")
ADJUST = reply(action="adjust_next_budget", message="Raise it to $135.")


# routes read the hooks at call time: the test db, one in-memory checkpointer, and scripted models in order
@pytest.fixture
def api(client, db, monkeypatch, tmp_path):
    ck, models = InMemorySaver(), []
    monkeypatch.setattr(budget_hooks, "session_factory", db)
    monkeypatch.setattr(budget_hooks, "get_checkpointer", lambda: ck)
    monkeypatch.setattr(budget_hooks, "make_model", lambda: models.pop(0) if models else ScriptedChatModel(replies=[]))
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")
    # runs finish before POST /budget/run returns: the in-memory test db is one sqlite connection, which a
    # background thread and the polling requests can't use at the same time
    from app.api import budget as budget_routes
    monkeypatch.setattr(budget_routes, "_spawn", lambda work: work())

    def seed(pid):
        with db() as s:
            uid = seed_profile(s, next(p for p in PLANTED if p.id == pid))
        monkeypatch.setattr(config, "DEV_USER_EMAIL", f"{pid}@eval.local")
        return uid

    def run(uid, *replies, checkpointer=None):
        return run_budget(db, uid, ONE, model=ScriptedChatModel(replies=list(replies)), checkpointer=checkpointer,
                          audit_path=tmp_path / "audit.jsonl")

    return SimpleNamespace(client=client, db=db, ck=ck, models=models, seed=seed, run=run, mp=monkeypatch)


def alert_ids(db):
    with db() as s:
        return sorted(a.id for a in s.query(Alert))


def test_alerts_newest_first_and_filter(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    [first] = alert_ids(api.db)
    decide_alert(api.db, uid, first, "accepted")
    api.run(uid, DIAG, ALERT)
    second = max(alert_ids(api.db))
    assert [a["id"] for a in api.client.get("/alerts").json()] == [second, first]
    pending = api.client.get("/alerts", params={"status": "pending_user"}).json()
    assert [a["id"] for a in pending] == [second]
    a = pending[0]
    assert (a["kind"], a["category"], a["severity"], a["cause"], a["action"], a["status"], a["period_start"]) == (
        "overspend", "GENERAL_MERCHANDISE", "high", "one_time", "alert", "pending_user", "2026-10-01")
    assert a["proposed_limit_cents"] is None and a["awaiting_approval"] is False


def test_alerts_bad_status_422(api):
    api.seed("overspend-one-time")
    assert api.client.get("/alerts", params={"status": "nope"}).status_code == 422


def test_alerts_only_current_user(api):
    other = api.seed("overspend-recurring")
    api.run(other, RECUR, reply(action="alert", message="Gym."))
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    assert [a["category"] for a in api.client.get("/alerts").json()] == ["GENERAL_MERCHANDISE"]


def test_alerts_awaiting_approval_flag(api):
    uid = api.seed("overspend-recurring")
    api.run(uid, RECUR, ADJUST, checkpointer=api.ck)
    [a] = api.client.get("/alerts").json()
    assert a["awaiting_approval"] is True and a["proposed_limit_cents"] == 13500


def test_accept_200_returns_alert(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    [aid] = alert_ids(api.db)
    r = api.client.post(f"/alerts/{aid}/accept")
    assert r.status_code == 200 and r.json()["id"] == aid and r.json()["status"] == "accepted"


def test_dismiss_200(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    [aid] = alert_ids(api.db)
    r = api.client.post(f"/alerts/{aid}/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "dismissed"


def test_accept_resumes_paused_run(api):
    uid = api.seed("overspend-recurring")
    state = api.run(uid, RECUR, ADJUST, checkpointer=api.ck)
    [aid] = alert_ids(api.db)
    assert api.client.post(f"/alerts/{aid}/accept").status_code == 200
    assert api.client.get("/alerts").json()[0]["awaiting_approval"] is False
    from app.db.models import AgentRun
    with api.db() as s:
        assert s.get(AgentRun, state["run_id"]).status == "ok"


def test_accept_other_users_404(api):
    other = api.seed("overspend-recurring")
    api.run(other, RECUR, reply(action="alert", message="Gym."))
    [aid] = alert_ids(api.db)
    api.seed("overspend-one-time")
    r = api.client.post(f"/alerts/{aid}/accept")
    assert (r.status_code, r.json()["detail"]) == (404, "Alert not found.")


def test_accept_twice_409(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    [aid] = alert_ids(api.db)
    api.client.post(f"/alerts/{aid}/accept")
    r = api.client.post(f"/alerts/{aid}/dismiss")
    assert (r.status_code, r.json()["detail"]) == (409, "This alert was already decided.")


def test_dev_user_email_override_acts_as_user(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    assert len(api.client.get("/alerts").json()) == 1
    api.mp.setattr(config, "DEV_USER_EMAIL", "dev@localhost")
    assert api.client.get("/alerts").json() == []


def test_unknown_dev_user_email_404(api):
    api.mp.setattr(config, "DEV_USER_EMAIL", "nobody@example.com")
    r = api.client.get("/alerts")
    assert (r.status_code, r.json()["detail"]) == (404, "Unknown DEV_USER_EMAIL user.")
    with api.db() as s:
        assert s.query(User).filter_by(email="nobody@example.com").count() == 0


def test_default_dev_user_still_created(api):
    api.mp.setattr(config, "DEV_USER_EMAIL", "dev@localhost")
    assert api.client.get("/alerts").status_code == 200
    with api.db() as s:
        assert s.query(User).filter_by(email="dev@localhost").count() == 1


# --- /budget and background runs ---

from app.api import budget as budget_api
from app.budget_engine.pace import BudgetLine as EngineLine, evaluate
from app.budget_service.initialize import load_txns
from app.db.models import AgentRun, BudgetLine, BudgetPeriod, PlaidItem


def wait(api, run_id, timeout=10.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = api.client.get(f"/budget/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("run never finished")


def start(api, when="2026-10-20"):
    r = api.client.post("/budget/run", json={"date": when})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def test_budget_lines_match_evaluate(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    api.mp.setattr(budget_api, "_today", lambda: ONE)
    body = api.client.get("/budget").json()
    with api.db() as s:
        period = s.query(BudgetPeriod).one()
        expected = evaluate([EngineLine(ln.category, ln.kind, ln.limit_cents) for ln in period.lines],
                            load_txns(s, uid, period.start_date, ONE), ONE)
    assert body["period"] == {"start": "2026-10-01", "end": "2026-10-31"}
    assert (body["days_elapsed"], body["days_in_period"]) == (20, 31)
    budgeted = [r for r in expected if r.kind != "unbudgeted"]
    assert [(ln["category"], ln["spent_cents"], ln["limit_cents"], ln["flagged"], ln["reason"]) for ln in body["lines"]] \
        == [(r.category, r.spent_cents, r.limit_cents, r.flagged, r.reason) for r in budgeted]


def test_budget_no_period_null(api):
    api.seed("overspend-one-time")
    api.mp.setattr(budget_api, "_today", lambda: ONE)
    body = api.client.get("/budget").json()
    assert (body["period"], body["lines"], body["unbudgeted"]) == (None, [], [])


def test_budget_period_over_is_null(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    api.mp.setattr(budget_api, "_today", lambda: date(2026, 11, 3))
    assert api.client.get("/budget").json()["period"] is None


def test_budget_writes_nothing(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    api.mp.setattr(budget_api, "_today", lambda: date(2026, 10, 25))
    with api.db() as s:
        before = [(ln.spent_cents, ln.last_pace) for ln in s.query(BudgetLine).order_by(BudgetLine.id)]
        counts = (s.query(AgentRun).count(), s.query(Alert).count())
    api.client.get("/budget")
    with api.db() as s:
        assert [(ln.spent_cents, ln.last_pace) for ln in s.query(BudgetLine).order_by(BudgetLine.id)] == before
        assert (s.query(AgentRun).count(), s.query(Alert).count()) == counts


def test_run_202_then_ok(api):
    api.seed("overspend-one-time")
    api.models.append(ScriptedChatModel(replies=[DIAG, ALERT]))
    body = wait(api, start(api))
    assert (body["status"], len(body["alert_ids"]), body["waiting_for"], body["error"]) == ("ok", 1, [], None)


def test_run_409_while_locked(api):
    uid = api.seed("overspend-one-time")
    lock = budget_api._user_lock(uid)
    lock.acquire()
    try:
        r = api.client.post("/budget/run", json={"date": "2026-10-20"})
        assert (r.status_code, r.json()["detail"]) == (409, "A budget run is already in progress.")
    finally:
        lock.release()


# an ollama outage ends in fallback alerts since the review on #12, so the crash comes from the engine
def test_run_error_status(api):
    from app.agents import budget_graph

    def boom(*a, **k):
        raise RuntimeError("bug")

    api.seed("overspend-one-time")
    api.mp.setattr(budget_graph, "evaluate", boom)
    api.models.append(ScriptedChatModel(replies=[]))
    body = wait(api, start(api))
    assert (body["status"], body["error"]) == ("error", "RuntimeError")
    assert api.client.post("/budget/run", json={"date": "2026-10-20"}).status_code == 202   # lock released


def test_run_syncs_only_real_items(api):
    uid = api.seed("overspend-one-time")
    calls = []
    api.mp.setattr(budget_api, "plaid_sync", lambda s, user_id: calls.append(user_id) or {"items": 1, "errors": []})
    api.models.append(ScriptedChatModel(replies=[DIAG, ALERT]))
    wait(api, start(api))
    assert calls == []
    with api.db() as s:
        s.query(PlaidItem).filter_by(user_id=uid).one().access_token_enc = "enc"
        s.commit()
    api.models.append(ScriptedChatModel(replies=[DIAG, reply(action="no_op", message="Already told you.")]))
    wait(api, start(api))
    assert calls == [uid]


def test_run_awaiting_user(api):
    api.seed("overspend-recurring")
    api.models.append(ScriptedChatModel(replies=[RECUR, ADJUST]))
    body = wait(api, start(api))
    assert body["status"] == "awaiting_user", body
    assert body["waiting_for"] == body["alert_ids"] and len(body["alert_ids"]) == 1


def test_run_other_users_404(api):
    api.seed("overspend-recurring")
    api.models.append(ScriptedChatModel(replies=[RECUR, ADJUST]))
    run_id = start(api)
    wait(api, run_id)
    api.seed("overspend-one-time")
    r = api.client.get(f"/budget/runs/{run_id}")
    assert (r.status_code, r.json()["detail"]) == (404, "Run not found.")


def test_run_bad_date_422(api):
    api.seed("overspend-one-time")
    assert api.client.post("/budget/run", json={"date": "2026-13-01"}).status_code == 422


# --- review fixes ---

def test_run_lock_released_when_start_fails(api):
    uid = api.seed("overspend-one-time")

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    api.mp.setattr(budget_api, "has_real_plaid_item", broken)
    r = api.client.post("/budget/run", json={"date": "2026-10-20"})
    assert (r.status_code, r.json()["detail"]) == (500, "The budget run couldn't start.")
    lock = budget_api._user_lock(uid)
    assert lock.acquire(blocking=False)        # free again, so the next run isn't stuck on 409
    lock.release()


def test_run_that_never_started_is_500(api):
    api.seed("overspend-one-time")

    def no_model():
        raise RuntimeError("ollama client misconfigured")

    api.mp.setattr(budget_hooks, "make_model", no_model)
    r = api.client.post("/budget/run", json={"date": "2026-10-20"})
    assert (r.status_code, r.json()["detail"]) == (500, "The budget run couldn't start.")


def test_budget_ended_period_reported(api):
    uid = api.seed("overspend-one-time")
    api.run(uid, DIAG, ALERT)
    api.mp.setattr(budget_api, "_today", lambda: date(2026, 11, 3))
    body = api.client.get("/budget").json()
    assert body["period"] is None
    assert body["ended_period"] == {"start": "2026-10-01", "end": "2026-10-31"}
    api.mp.setattr(budget_api, "_today", lambda: ONE)
    assert api.client.get("/budget").json()["ended_period"] is None


def test_startup_closes_interrupted_runs(api):
    from fastapi.testclient import TestClient
    from app.main import app
    uid = api.seed("overspend-one-time")
    with api.db() as s:
        s.add_all([AgentRun(user_id=uid, graph="budget", status="running"),
                   AgentRun(user_id=uid, graph="budget", status="awaiting_user")])
        s.commit()
    with TestClient(app):          # runs the app's startup
        pass
    with api.db() as s:
        runs = {r.status: r.summary_json for r in s.query(AgentRun)}
    assert runs == {"error": {"error": "Interrupted"}, "awaiting_user": {}}


# review on #13: an unreadable or missing checkpoint is reported, not a 500 or "waiting on 0 proposals"
def test_run_status_with_a_broken_checkpoint(api):
    api.seed("overspend-recurring")
    api.models.append(ScriptedChatModel(replies=[RECUR, ADJUST]))
    run_id = start(api)
    wait(api, run_id)

    class Unreadable(InMemorySaver):
        def get_tuple(self, config):
            raise RuntimeError("no such table")

    for ck in (InMemorySaver(), Unreadable()):
        api.mp.setattr(budget_hooks, "get_checkpointer", lambda ck=ck: ck)
        r = api.client.get(f"/budget/runs/{run_id}")
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["waiting_for"]) == ("error", [])


# review on #13: deciding never runs a model, so a broken model setting can't block a decision
def test_decide_needs_no_model(api):
    uid = api.seed("overspend-recurring")
    api.run(uid, RECUR, ADJUST, checkpointer=api.ck)
    [aid] = alert_ids(api.db)

    def no_model():
        raise RuntimeError("bad OLLAMA_MODEL")

    api.mp.setattr(budget_hooks, "make_model", no_model)
    assert api.client.post(f"/alerts/{aid}/accept").status_code == 200
