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
