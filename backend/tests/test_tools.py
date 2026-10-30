import json
from datetime import date

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import MemorySaver

from app import config
from app.agents.chat_graph import build_chat_graph
from app.agents.tools import PFC_CATEGORIES, make_tools
from app.db.models import Account, Transaction
from app.privacy import gateway as gw
from app.privacy.gateway import PrivacyGateway
from tests.factories import add_account, add_txn, add_user
from tests.fakes import FakeKB, ollama_has

TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(gw, "_today", lambda: TODAY)


# a user with a checking account, two debts and some spending; plus another user who must stay invisible
@pytest.fixture
def seeded(db):
    with db() as s:
        item = add_user(s, "u-1", "one@example.com")
        checking = add_account(s, item, "Plaid Checking", balance=11000)
        add_account(s, item, "Plaid Credit Card", type_="credit", balance=41000,
                    liability={"kind": "credit", "apr": 22.0, "min_payment_cents": 2000})
        add_account(s, item, "Plaid Student Loan", type_="loan", balance=650000,
                    liability={"kind": "student", "apr": 5.25})
        add_txn(s, checking, 650, "FOOD_AND_DRINK", merchant="Starbucks", day=date(2026, 10, 10))
        add_txn(s, checking, 12000, "TRAVEL", merchant="Delta", day=date(2026, 10, 12))
        other = add_user(s, "u-2", "two@example.com")
        add_account(s, other, "Their Secret Card", type_="credit", liability={"kind": "credit", "apr": 30.0})
        s.commit()
    return db


@pytest.fixture
def kb():
    return FakeKB()


# tools built for user u-1, as the chat session would build them
@pytest.fixture
def tools(seeded, tmp_path, kb):
    with seeded() as s:
        yield {t.name: t for t in make_tools(PrivacyGateway(s, "u-1", audit_path=tmp_path / "audit.jsonl"), kb)}


def data(out: str):
    return json.loads(out)["data"]


def test_four_tools_with_descriptions(tools):
    assert set(tools) == {"get_accounts", "get_debts", "get_spending", "search_kb"}
    assert all(t.description for t in tools.values())


# the model can never pick whose data it reads: no tool takes a user id
def test_no_tool_accepts_a_user(tools):
    for t in tools.values():
        assert not any("user" in arg for arg in t.args)
    assert set(tools["get_spending"].args) == {"category", "start_date", "end_date"}
    assert tools["get_accounts"].args == {} and tools["get_debts"].args == {}
    assert set(tools["search_kb"].args) == {"query"}


def test_get_accounts(tools):
    names = [a["name"] for a in data(tools["get_accounts"].invoke({}))]

    assert names == ["Plaid Checking", "Plaid Credit Card", "Plaid Student Loan"]


def test_get_debts_highest_apr_first(tools):
    debts = data(tools["get_debts"].invoke({}))

    assert [(d["name"], d["apr"]) for d in debts] == [("Plaid Credit Card", 22.0), ("Plaid Student Loan", 5.25)]


def test_other_users_data_hidden(tools):
    outputs = tools["get_accounts"].invoke({}) + tools["get_debts"].invoke({})

    assert "Their Secret Card" not in outputs


def test_get_spending_defaults_to_totals(tools):
    assert data(tools["get_spending"].invoke({})) == {"TRAVEL": 120.0, "FOOD_AND_DRINK": 6.5}


def test_get_spending_category_and_dates(tools):
    out = data(tools["get_spending"].invoke(
        {"category": "food_and_drink", "start_date": "2026-10-01", "end_date": "2026-10-11"}))

    assert out["category"] == "FOOD_AND_DRINK"
    assert out["total"] == 6.5
    assert [t["merchant"] for t in out["transactions"]] == ["Starbucks"]


# a word that isn't a plaid category must not read as "$0 spent"; the error teaches the valid names
def test_unknown_category_lists_valid_ones(tools):
    with pytest.raises(ToolException) as err:
        tools["get_spending"].invoke({"category": "groceries"})

    assert "groceries" in str(err.value)
    assert "FOOD_AND_DRINK" in str(err.value)


@pytest.mark.parametrize("args", [{"start_date": "10/01/2026"}, {"end_date": "yesterday"}])
def test_bad_date_explains_format(tools, args):
    with pytest.raises(ToolException, match="YYYY-MM-DD"):
        tools["get_spending"].invoke(args)


def test_reversed_range_rejected(tools):
    with pytest.raises(ToolException, match="before"):
        tools["get_spending"].invoke({"start_date": "2026-10-20", "end_date": "2026-10-01"})


def test_category_list_is_plaids(tools):
    assert len(PFC_CATEGORIES) == 16
    assert "FOOD_AND_DRINK" in tools["get_spending"].description



# review on #7: with only a start date the gateway fills in today, so a future start is reversed too
def test_future_start_alone_rejected(tools):
    with pytest.raises(ToolException, match="before"):
        tools["get_spending"].invoke({"start_date": "2026-11-01"})


# small models send "" for optional arguments they mean to leave out
def test_empty_string_args_mean_default(tools):
    out = tools["get_spending"].invoke({"category": "", "start_date": " ", "end_date": ""})

    assert data(out) == {"TRAVEL": 120.0, "FOOD_AND_DRINK": 6.5}


# the totals can show UNCATEGORIZED, so the model is told it may ask for it; money in is labelled as such
def test_description_and_error_name_uncategorized(tools):
    with pytest.raises(ToolException) as err:
        tools["get_spending"].invoke({"category": "groceries"})

    assert "UNCATEGORIZED" in str(err.value)
    assert "UNCATEGORIZED" in tools["get_spending"].description
    assert "money in" in tools["get_spending"].description


# the factory's txn ids must stay unique across sessions, or a second session's rows collide
def test_factory_txn_ids_unique_across_sessions(db):
    with db() as s:
        acct_id = add_account(s, add_user(s, "u-1", "a@example.com"), "Checking").id
        add_txn(s, s.get(Account, acct_id), 100, "FOOD_AND_DRINK")
        s.commit()
    with db() as s:
        add_txn(s, s.get(Account, acct_id), 200, "FOOD_AND_DRINK")
        s.commit()
        assert s.query(Transaction).count() == 2

# tools reach data only through the gateway, so every call is in the audit log
def test_every_call_is_audited(tools, tmp_path):
    tools["get_accounts"].invoke({})
    tools["get_debts"].invoke({})
    tools["get_spending"].invoke({"category": "TRAVEL"})

    lines = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [l["purpose"] for l in lines] == ["accounts", "debts", "spending"]


# review on #9: the system prompt says never write URLs; the tool description must not say the opposite
def test_search_kb_description_does_not_ask_for_urls(tools):
    description = tools["search_kb"].description.lower()

    assert "cite" not in description and "url" not in description

def test_search_kb_output_shape(tools, kb):
    out = tools["search_kb"].invoke({"query": "which debt should I pay first"})

    assert json.loads(out) == {"purpose": "kb", "results": [FakeKB.RESULT]}
    assert kb.searches == [("which debt should I pay first", 4)]


@pytest.mark.parametrize("query", ["", "   "])
def test_search_kb_rejects_empty_query(tools, query):
    with pytest.raises(ToolException, match="query must not be empty"):
        tools["search_kb"].invoke({"query": query})


# a model that pastes the whole conversation in as the query still gets a search
def test_search_kb_trims_long_query(tools, kb):
    tools["search_kb"].invoke({"query": "x" * 5000})

    assert len(kb.searches[0][0]) == 1000


def test_search_kb_empty_kb(seeded, tmp_path):
    kb = FakeKB(chunks=0)
    with seeded() as s:
        search = {t.name: t for t in make_tools(PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl"), kb)}

        with pytest.raises(ToolException, match="knowledge base is empty"):
            search["search_kb"].invoke({"query": "roth ira"})
    assert kb.searches == []


# the ticket's "learn" goal: can the local model pick the right tool from these schemas alone?
@pytest.mark.skipif(not ollama_has(config.OLLAMA_MODEL), reason="needs ollama running with OLLAMA_MODEL pulled")
def test_live_model_picks_get_debts(seeded, tmp_path):
    audit = tmp_path / "audit.jsonl"
    with seeded() as s:
        tools = make_tools(PrivacyGateway(s, "u-1", audit_path=audit), FakeKB())
        graph = build_chat_graph(tools, checkpointer=MemorySaver())
        graph.invoke({"messages": [HumanMessage("Which of my debts has the highest APR?")], "tool_rounds": 0},
                     {"configurable": {"thread_id": "t1"}})
        answer = graph.get_state({"configurable": {"thread_id": "t1"}}).values["messages"][-1]

    # no audit file means the model called no tool; fail on that, not on FileNotFoundError
    purposes = [json.loads(l)["purpose"] for l in audit.read_text().splitlines()] if audit.exists() else []
    assert "debts" in purposes
    assert "credit card" in answer.content.lower()
