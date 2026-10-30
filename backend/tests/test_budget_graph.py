import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from app import config
from app.agents import budget_graph
from app.agents.budget_graph import run_budget
from app.agents.llm import get_chat_model
from app.db.models import Account, AdviceLog, AgentRun, Alert, BudgetLine, BudgetPeriod
from eval.budget_users import PLANTED, PROFILES, seed_profile
from tests.factories import add_account, add_txn, add_user
from tests.fakes import ScriptedChatModel, ollama_has

ONE = date(2026, 10, 20)
GM = "GENERAL_MERCHANDISE"


def seed(db, pid):
    profile = next(p for p in PLANTED + PROFILES if p.id == pid)
    with db() as s:
        return seed_profile(s, profile)


def reply(**kw):
    return AIMessage(content=json.dumps(kw))


DIAG = reply(cause="one_time", evidence="One $600 purchase at ElectroMart explains most of it.")
ALERT = reply(action="alert", message="A $600 ElectroMart purchase put you over your limit.")


def fake(*replies):
    return ScriptedChatModel(replies=list(replies))


def run(db, uid, today=ONE, model=None, tmp_path=None, **kw):
    return run_budget(db, uid, today, model=model or fake(), audit_path=tmp_path / "audit.jsonl", **kw)


def rows(db, cls, **filters):
    with db() as s:
        return s.query(cls).filter_by(**filters).all()


def test_full_run_one_time(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    state = run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path)
    [alert] = rows(db, Alert, user_id=uid)
    assert (alert.kind, alert.severity, alert.status, alert.category) == ("overspend", "high", "pending_user", GM)
    assert (alert.cause, alert.action, alert.proposed_limit_cents) == ("one_time", "alert", None)
    assert alert.message.startswith("A $600 ElectroMart")
    [advice] = rows(db, AdviceLog, user_id=uid)
    assert (advice.run_id, advice.category, advice.advice_text) == (state["run_id"], GM, alert.message)
    [agent_run] = rows(db, AgentRun, user_id=uid)
    assert (agent_run.graph, agent_run.status) == ("budget", "ok") and agent_run.finished_at is not None
    assert set(agent_run.summary_json) == {"period_id", "flags", "causes", "actions", "alert_ids", "unbudgeted",
                                           "uncategorized", "sync", "llm_failures"}
    gm_line = next(ln for ln in rows(db, BudgetLine) if ln.category == GM)
    assert gm_line.spent_cents > 60000 and gm_line.last_pace is not None


def test_second_run_reuses_budget_and_alerts_again(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    run(db, uid, model=fake(DIAG, ALERT, DIAG, ALERT), tmp_path=tmp_path)
    run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path)
    assert (len(rows(db, BudgetPeriod)), len(rows(db, Alert)), len(rows(db, AgentRun))) == (1, 2, 2)


def test_no_history_no_budget(db, tmp_path):
    with db() as s:
        acct = add_account(s, add_user(s, "u1", "u1@x"), "Checking")
        add_txn(s, acct, 5000, "FOOD_AND_DRINK", day=date(2026, 10, 3))
        s.commit()
    m = fake()
    state = run(db, "u1", model=m, tmp_path=tmp_path)
    assert state["status"] == "no_budget" and m.seen == [] and rows(db, Alert) == []
    assert rows(db, AgentRun)[0].status == "no_budget"


def test_period_ended(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path)
    assert run(db, uid, date(2026, 11, 3), tmp_path=tmp_path)["status"] == "period_ended"
    assert len(rows(db, Alert)) == 1


def test_last_day_of_period_runs(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    assert run(db, uid, date(2026, 10, 31), model=fake(DIAG, ALERT), tmp_path=tmp_path)["status"] == "ok"


def test_no_flags_no_llm(db, tmp_path):
    with db() as s:
        acct = add_account(s, add_user(s, "u1", "u1@x"), "Checking")
        for m in (7, 8, 9, 10):
            add_txn(s, acct, 150000, "RENT_AND_UTILITIES", day=date(2026, m, 1))
        s.commit()
    m = fake()
    assert run(db, "u1", model=m, tmp_path=tmp_path)["status"] == "ok"
    assert m.seen == []
    assert rows(db, BudgetLine)[0].spent_cents == 150000


def test_adjust_stores_proposed_limit(db, tmp_path):
    uid = seed(db, "overspend-recurring")
    run(db, uid, model=fake(reply(cause="recurring", evidence="A new $90 gym."),
                            reply(action="adjust_next_budget", message="Raise personal care to $135.")),
        tmp_path=tmp_path)
    [alert] = rows(db, Alert)
    # the old $42 limit plus the new $90 gym's typical month, up to $5 (not $130 projected straight-line)
    assert (alert.action, alert.proposed_limit_cents, alert.severity) == ("adjust_next_budget", 13500, "high")


def test_no_op_writes_nothing(db, tmp_path):
    uid = seed(db, "overspend-recurring")
    state = run(db, uid, model=fake(reply(cause="recurring", evidence="A gym."), reply(action="no_op", message="Fine.")),
                tmp_path=tmp_path)
    assert rows(db, Alert) == [] and rows(db, AdviceLog) == []
    assert state["actions"]["PERSONAL_CARE"]["action"] == "no_op"


def test_diagnose_failure_still_alerts(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    state = run(db, uid, model=fake(AIMessage(content="bad"), AIMessage(content="bad"), ALERT), tmp_path=tmp_path)
    [alert] = rows(db, Alert)
    assert alert.cause is None and alert.action == "alert"
    assert state["llm_failures"] == 1 and state["diagnoses"][GM] is None


def test_decide_failure_fallback(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    state = run(db, uid, model=fake(DIAG, AIMessage(content="bad"), AIMessage(content="bad")), tmp_path=tmp_path)
    [alert] = rows(db, Alert)
    assert alert.action == "alert" and alert.cause == "one_time"
    assert alert.message.startswith("General merchandise is over its $") and "limit: $" in alert.message
    assert state["actions"][GM]["fallback"] is True and state["llm_failures"] == 1


def test_adjust_downgraded_unless_recurring(db, tmp_path):
    # live, holiday shopping got "adjust_next_budget" with a $1,600 january limit; only a recurring cause may adjust
    uid = seed(db, "overspend-one-time")
    state = run(db, uid, model=fake(reply(cause="seasonal", evidence="Holiday gifts."),
                                    reply(action="adjust_next_budget", message="I propose a new limit of $1600.00.")),
                tmp_path=tmp_path)
    [alert] = rows(db, Alert)
    assert (alert.action, alert.proposed_limit_cents) == ("alert", None)
    assert alert.message.startswith("General merchandise is over its $") and "1600" not in alert.message
    assert state["actions"][GM]["downgraded"] is True


def test_sync_called_and_failure_recorded(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    calls = []

    def sync_ok(session, user_id):
        calls.append(user_id)
        return {"items": 1, "errors": []}

    def sync_boom(session, user_id):
        raise RuntimeError("plaid down")

    run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path, sync_fn=sync_ok)
    assert calls == [uid]
    state = run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path, sync_fn=sync_boom)
    assert state["status"] == "ok"
    assert rows(db, AgentRun)[-1].summary_json["sync"] == {"items": 0, "errors": ["RuntimeError"]}


def test_no_sync_fn_skipped(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    assert run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path)["sync"] == {"skipped": True}


def test_crash_marks_error(db, tmp_path, monkeypatch):
    uid = seed(db, "overspend-one-time")

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(budget_graph, "evaluate", boom)
    with pytest.raises(RuntimeError):
        run(db, uid, tmp_path=tmp_path)
    [agent_run] = rows(db, AgentRun)
    assert (agent_run.status, agent_run.summary_json) == ("error", {"error": "RuntimeError"})
    assert agent_run.finished_at is not None


def test_prompt_privacy(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    m = fake(DIAG, ALERT)
    run(db, uid, model=m, tmp_path=tmp_path)
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [a["purpose"] for a in audit] == ["diagnose"]
    for call in m.seen:
        text = " ".join(str(msg.content) for msg in call)
        assert audit[0]["pseudonym"] in text and "ElectroMart" in text
        assert uid not in text


def test_unbudgeted_listed(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    with db() as s:
        s.info["n"] = 100000     # add_txn numbers plaid ids per session; keep clear of the seeded ones
        add_txn(s, s.query(Account).filter_by(user_id=uid).one(), 20000, "TRAVEL", day=date(2026, 10, 15))
        s.commit()
    state = run(db, uid, model=fake(DIAG, ALERT), tmp_path=tmp_path)
    assert "TRAVEL" in rows(db, AgentRun)[0].summary_json["unbudgeted"]
    assert [a.category for a in rows(db, Alert)] == [GM]
    assert state["status"] == "ok"


@pytest.mark.skipif(not ollama_has(config.OLLAMA_MODEL), reason="needs ollama running with OLLAMA_MODEL pulled")
def test_live_planted_causes(db, tmp_path):
    model = get_chat_model()
    for p in PLANTED:
        state = run(db, seed(db, p.id), p.run_date, model=model, tmp_path=tmp_path)
        [flag] = state["flags"]
        cat = flag["category"]
        print(p.id, state["diagnoses"][cat], state["actions"][cat])
        assert state["diagnoses"][cat] is not None, p.id
        assert state["diagnoses"][cat]["cause"] == p.expected_cause, p.id
        # only a recurring cause may change next month's budget (the guard downgrades anything else)
        if p.expected_cause == "recurring":
            assert state["actions"][cat]["action"] == "adjust_next_budget", p.id
        else:
            assert state["actions"][cat]["action"] not in ("adjust_next_budget", "no_op"), p.id


# review on #12: ollama down or timing out must not hide an overspend; each flag still gets a plain alert
def test_model_unreachable_still_alerts(db, tmp_path):
    uid = seed(db, "overspend-one-time")
    state = run(db, uid, model=fake(ConnectionError("down"), ConnectionError("down")), tmp_path=tmp_path)
    [alert] = rows(db, Alert)
    assert alert.action == "alert" and alert.cause is None
    assert alert.message.startswith("General merchandise is over its $")
    assert state["status"] == "ok" and state["llm_failures"] == 2


# review on #12: a user with no budget yet is synced first, so the budget can be fit on fresh data
def test_no_budget_syncs_before_fitting(db, tmp_path):
    with db() as s:
        add_account(s, add_user(s, "u1", "u1@x"), "Checking")
        s.commit()

    def sync_history(session, user_id):
        acct = session.query(Account).filter_by(user_id=user_id).one()
        for m in (7, 8, 9):
            add_txn(session, acct, 40000, "FOOD_AND_DRINK", day=date(2026, m, 2))
        session.commit()
        return {"items": 1, "errors": []}

    state = run(db, "u1", model=fake(), tmp_path=tmp_path, sync_fn=sync_history)
    assert state["status"] == "ok"
    assert len(rows(db, BudgetPeriod, user_id="u1")) == 1
