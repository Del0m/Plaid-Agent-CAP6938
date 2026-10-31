import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.budget_graph import run_budget
from app.budget_service import alerts as svc
from app.budget_service.alerts import (AlertAlreadyDecided, AlertNotFound, awaiting_approval, decide_alert,
                                       has_real_plaid_item)
from app.db.models import AdviceLog, AgentRun, Alert, PlaidItem
from eval.budget_users import PLANTED, seed_profile
from tests.factories import add_user
from tests.fakes import ScriptedChatModel

ONE = date(2026, 10, 20)


def reply(**kw):
    return AIMessage(content=json.dumps(kw))


def seed(db, pid):
    with db() as s:
        return seed_profile(s, next(p for p in PLANTED if p.id == pid))


# one-time user: a plain alert, no paused run
def plain_alert(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    run_budget(db, uid, ONE, audit_path=tmp_path / "a.jsonl", model=ScriptedChatModel(replies=[
        reply(cause="one_time", evidence="A $600 purchase."), reply(action="alert", message="Over.")]))
    with db() as s:
        return uid, s.query(Alert).one().id


# gym user: an adjust_next_budget alert holding a paused run
def paused(db, tmp_path, ck):
    uid = seed(db, "overspend-recurring")
    state = run_budget(db, uid, ONE, audit_path=tmp_path / "a.jsonl", checkpointer=ck, model=ScriptedChatModel(
        replies=[reply(cause="recurring", evidence="A new gym."),
                 reply(action="adjust_next_budget", message="Raise it to $135.")]))
    return uid, state["waiting_for"][0], state["run_id"]


def get(db, cls, ident):
    with db() as s:
        return s.get(cls, ident)


def outcomes(db, alert_id):
    with db() as s:
        return [a.outcome for a in s.query(AdviceLog).filter_by(alert_id=alert_id)]


def test_accept_sets_status_and_outcome(db, tmp_path):
    uid, aid = plain_alert(db, tmp_path)
    assert decide_alert(db, uid, aid, "accepted").status == "accepted"
    assert get(db, Alert, aid).status == "accepted" and outcomes(db, aid) == ["accepted"]


def test_dismiss_sets_status_and_outcome(db, tmp_path):
    uid, aid = plain_alert(db, tmp_path)
    decide_alert(db, uid, aid, "dismissed")
    assert get(db, Alert, aid).status == "dismissed" and outcomes(db, aid) == ["dismissed"]


def test_already_decided(db, tmp_path):
    uid, aid = plain_alert(db, tmp_path)
    decide_alert(db, uid, aid, "accepted")
    with pytest.raises(AlertAlreadyDecided):
        decide_alert(db, uid, aid, "dismissed")
    assert get(db, Alert, aid).status == "accepted"


def test_other_user_or_unknown_not_found(db, tmp_path):
    uid, aid = plain_alert(db, tmp_path)
    with db() as s:
        add_user(s, "someone-else", "else@x")
        s.commit()
    with pytest.raises(AlertNotFound):
        decide_alert(db, "someone-else", aid, "accepted")
    with pytest.raises(AlertNotFound):
        decide_alert(db, uid, 99999, "accepted")
    assert get(db, Alert, aid).status == "pending_user"


def test_accept_resumes_paused_run(db, tmp_path):
    ck = InMemorySaver()
    uid, aid, run_id = paused(db, tmp_path, ck)
    decide_alert(db, uid, aid, "accepted", checkpointer=ck)
    assert get(db, AgentRun, run_id).status == "ok"
    assert get(db, AgentRun, run_id).summary_json["decisions"] == {str(aid): "accepted"}


def test_dismiss_resumes_too(db, tmp_path):
    ck = InMemorySaver()
    uid, aid, run_id = paused(db, tmp_path, ck)
    decide_alert(db, uid, aid, "dismissed", checkpointer=ck)
    assert get(db, AgentRun, run_id).summary_json["decisions"] == {str(aid): "dismissed"}


def test_resume_failure_keeps_decision(db, tmp_path, monkeypatch):
    ck = InMemorySaver()
    uid, aid, run_id = paused(db, tmp_path, ck)

    def boom(*a, **k):
        raise RuntimeError("checkpoint broken")

    monkeypatch.setattr(svc, "resume_budget_run", boom)
    assert decide_alert(db, uid, aid, "accepted", checkpointer=ck).status == "accepted"
    assert get(db, Alert, aid).status == "accepted"
    assert get(db, AgentRun, run_id).status == "awaiting_user"


def test_missing_checkpoint_still_decides(db, tmp_path):
    uid, aid, run_id = paused(db, tmp_path, InMemorySaver())
    empty = InMemorySaver()
    assert awaiting_approval(db, uid, checkpointer=empty) == set()
    assert decide_alert(db, uid, aid, "accepted", checkpointer=empty).status == "accepted"
    assert get(db, AgentRun, run_id).status == "awaiting_user"


def test_awaiting_approval_ids(db, tmp_path):
    ck = InMemorySaver()
    uid, aid, _ = paused(db, tmp_path, ck)
    assert awaiting_approval(db, uid, checkpointer=ck) == {aid}
    decide_alert(db, uid, aid, "accepted", checkpointer=ck)
    assert awaiting_approval(db, uid, checkpointer=ck) == set()


def test_has_real_plaid_item(db):
    uid = seed(db, "overspend-one-time")
    with db() as s:
        assert has_real_plaid_item(s, uid) is False
        s.query(PlaidItem).filter_by(user_id=uid).one().access_token_enc = "enc"
        s.commit()
        assert has_real_plaid_item(s, uid) is True


def test_failed_resume_keeps_run_awaiting(db, tmp_path, monkeypatch):
    # review finding: a resume that fails while the thread is still paused must leave the run waiting, not error
    from app.agents import budget_graph

    def broken(*a, **k):
        raise RuntimeError("resume broke before running")

    ck = InMemorySaver()
    uid, aid, run_id = paused(db, tmp_path, ck)
    monkeypatch.setattr(budget_graph, "Command", broken)
    assert decide_alert(db, uid, aid, "accepted", checkpointer=ck).status == "accepted"
    assert get(db, AgentRun, run_id).status == "awaiting_user"


# review on #13: a resume that failed leaves the run paused on a decided alert; the next look at alerts finishes it
def test_failed_resume_retried_on_next_load(db, tmp_path, monkeypatch):
    ck = InMemorySaver()
    uid, aid, run_id = paused(db, tmp_path, ck)
    real = svc.resume_budget_run

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(svc, "resume_budget_run", boom)
    decide_alert(db, uid, aid, "accepted", checkpointer=ck)
    assert get(db, AgentRun, run_id).status == "awaiting_user"

    monkeypatch.setattr(svc, "resume_budget_run", real)
    assert awaiting_approval(db, uid, checkpointer=ck) == set()
    assert get(db, AgentRun, run_id).status == "ok"
    assert get(db, AgentRun, run_id).summary_json["decisions"] == {str(aid): "accepted"}


# review on #13: a decision made after the run read its alerts but before it was marked awaiting_user
# (decide_alert only resumes awaiting runs) is picked up as soon as the run is marked
def test_decision_just_before_the_pause_is_picked_up(db, tmp_path, monkeypatch):
    from app.agents import budget_graph
    real_interrupt = budget_graph.interrupt

    def click_then_pause(value):
        for aid in value["waiting_for"]:
            decide_alert(db, uid, aid, "dismissed")      # the run row still says "running": nothing is resumed
        return real_interrupt(value)

    monkeypatch.setattr(budget_graph, "interrupt", click_then_pause)
    ck = InMemorySaver()
    uid = seed(db, "overspend-recurring")
    state = run_budget(db, uid, ONE, audit_path=tmp_path / "a.jsonl", checkpointer=ck, model=ScriptedChatModel(
        replies=[reply(cause="recurring", evidence="A new gym."), reply(action="adjust_next_budget", message="x")]))
    assert get(db, AgentRun, state["run_id"]).status == "ok"


# review on #13: two decisions for one paused run that land before any resume both reach it
def test_two_decisions_before_a_resume_both_count(db, tmp_path):
    ck = InMemorySaver()
    uid, aid, run_id = paused(db, tmp_path, ck)
    with db() as s:
        s.get(Alert, aid).status = "accepted"           # as if decided by a request whose resume never ran
        s.commit()
    assert awaiting_approval(db, uid, checkpointer=ck) == set()
    assert get(db, AgentRun, run_id).summary_json["decisions"] == {str(aid): "accepted"}
