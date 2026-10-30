import asyncio
import json
import logging
import re
import sqlite3
import threading
import time

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import select

from app import config
from app.agents import chat_graph
from app.api import chat
from app.api.deps import get_current_user
from app.db.models import User
from app.main import app
from app.rag.store import KnowledgeBase
from tests.factories import add_account, add_user
from tests.fakes import FakeKB, ScriptedChatModel, ollama_has, tool_call

QUESTION = "Which debt should I pay first?"
ANSWER = "Pay the credit card first."
SOURCE = {"title": FakeKB.RESULT["title"], "section": FakeKB.RESULT["section"], "url": FakeKB.RESULT["source_url"]}


def read_events(body: str) -> list:
    events = []
    for block in body.strip().split("\n\n"):
        if block.startswith(":"):   # sse comment (keep-alive)
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def types(events):
    return [e for e, _ in events]


# u-1 has a 22% card and a 5.25% loan; u-2 exists only to prove threads are per user
@pytest.fixture
def users(db):
    with db() as s:
        item = add_user(s, "u-1", "one@example.com")
        add_account(s, item, "Plaid Credit Card", type_="credit", balance=41000,
                    liability={"kind": "credit", "apr": 22.0, "min_payment_cents": 2000})
        add_account(s, item, "Plaid Student Loan", type_="loan", balance=650000,
                    liability={"kind": "student", "apr": 5.25})
        add_user(s, "u-2", "two@example.com")
        s.commit()
        return {u.id: u for u in s.scalars(select(User))}


@pytest.fixture
def model():
    return ScriptedChatModel(replies=[])


@pytest.fixture
def saver():
    return MemorySaver()


# the api with the test db, a fake kb and model, an in-memory checkpointer, and u-1 signed in
@pytest.fixture
def api(client, db, users, model, saver, monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "session_factory", db)
    monkeypatch.setattr(chat, "get_checkpointer", lambda: saver)
    monkeypatch.setattr(chat, "make_kb", lambda: FakeKB())
    monkeypatch.setattr(chat_graph, "get_chat_model", lambda **kw: model)
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")
    app.dependency_overrides[get_current_user] = lambda: users["u-1"]
    return client


def ask(api, message=QUESTION, thread_id="t1"):
    return api.post("/chat", json={"thread_id": thread_id, "message": message})


def test_turn_streams_tools_then_tokens_then_done(api, model):
    model.replies += [tool_call("get_debts"), AIMessage(ANSWER)]

    resp = ask(api)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = read_events(resp.text)
    assert types(events)[:3] == ["sources", "tool_call", "tool_result"]
    assert set(types(events)[3:-1]) == {"token"} and len(events) > 5
    assert types(events)[-1] == "done"
    assert events[0][1] == {"sources": [SOURCE]}
    assert events[1][1] == {"id": "c1", "name": "get_debts", "args": {}}
    assert events[2][1] == {"id": "c1", "name": "get_debts", "ok": True}
    assert "".join(d["text"] for e, d in events if e == "token") == ANSWER


# the browser shows which tools ran, never what they returned
def test_tool_result_content_not_sent(api, model):
    model.replies += [tool_call("get_debts"), AIMessage(ANSWER)]

    assert "Plaid Credit Card" not in ask(api).text


def test_failed_tool_reports_not_ok(api, model):
    model.replies += [tool_call("nope"), AIMessage("ok")]

    events = read_events(ask(api).text)

    assert ("tool_result", {"id": "c1", "name": "nope", "ok": False}) in events


@pytest.mark.parametrize("message", ["", "   ", "x" * 4001])
def test_message_validation(api, message):
    assert ask(api, message=message).status_code == 422


@pytest.mark.parametrize("thread_id", ["../x", "a b", "a" * 65, ""])
def test_thread_id_must_be_simple(api, thread_id):
    assert ask(api, thread_id=thread_id).status_code == 422


# a slash never reaches the route as one segment; spaces and length are caught by the pattern
@pytest.mark.parametrize("thread_id", ["a%20b", "a" * 65])
def test_history_thread_id_must_be_simple(api, thread_id):
    assert api.get(f"/chat/{thread_id}").status_code == 422


def test_ollama_down_message(api, model):
    model.replies.append(ConnectionError("refused"))

    events = read_events(ask(api).text)

    assert events == [("sources", {"sources": [SOURCE]}),
                      ("error", {"message": "The local model isn't reachable. Is Ollama running?"})]


# exception text can carry user data; it goes to the local log, never to the browser
def test_other_error_is_generic(api, model):
    model.replies.append(RuntimeError("acct 4321"))

    resp = ask(api)

    assert read_events(resp.text)[-1] == ("error", {"message": "Something went wrong answering that. Try again."})
    assert "4321" not in resp.text


def test_empty_answer_message(api, model):
    model.replies.append(AIMessage(content=""))

    events = read_events(ask(api).text)

    assert events[-1] == ("error", {"message": "The model returned an empty answer. Try rephrasing."})
    assert "token" not in types(events)


# json escapes the newlines, so a blank line in the answer can't end the sse event early
def test_token_with_blank_lines_is_one_event(api, model):
    model.replies.append(AIMessage("a\n\nb"))

    events = read_events(ask(api).text)

    assert "".join(d["text"] for e, d in events if e == "token") == "a\n\nb"
    assert types(events)[-1] == "done"


def test_history_grouped_after_turn(api, model):
    model.replies += [tool_call("get_debts"), AIMessage(ANSWER)]
    ask(api)

    assert api.get("/chat/t1").json() == [
        {"role": "user", "text": QUESTION, "tools": []},
        {"role": "assistant", "text": ANSWER, "tools": [{"name": "get_debts", "args": {}, "ok": True}],
         "sources": [SOURCE]},
    ]


def test_no_sources_event_when_kb_empty(api, model, monkeypatch):
    monkeypatch.setattr(chat, "make_kb", lambda: FakeKB(chunks=0))
    model.replies.append(AIMessage(ANSWER))

    events = read_events(ask(api).text)

    assert "sources" not in types(events)
    assert api.get("/chat/t1").json()[1]["sources"] == []


# qwen sometimes writes text next to a tool call; that text is not the answer
def test_text_before_tool_call_not_in_history(api, model):
    call = tool_call("get_debts")
    call.content = "CallCheck_debts()"
    model.replies += [call, AIMessage(ANSWER)]

    events = read_events(ask(api).text)

    assert types(events).index("token") < types(events).index("tool_call")   # the client drops it
    assert api.get("/chat/t1").json()[1]["text"] == ANSWER


def test_unknown_thread_history_empty(api):
    assert api.get("/chat/never-used").json() == []


def test_threads_are_per_user(api, model, users):
    model.replies.append(AIMessage(ANSWER))
    ask(api)

    app.dependency_overrides[get_current_user] = lambda: users["u-2"]

    assert api.get("/chat/t1").json() == []


def test_history_survives_new_checkpointer(api, model, monkeypatch, tmp_path):
    path = tmp_path / "cp.db"
    first = SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    monkeypatch.setattr(chat, "get_checkpointer", lambda: first)
    model.replies += [tool_call("get_debts"), AIMessage(ANSWER), tool_call("get_debts", id="c2"), AIMessage("again")]
    ask(api)
    before = api.get("/chat/t1").json()

    # a backend restart: a fresh saver on the same file
    second = SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    monkeypatch.setattr(chat, "get_checkpointer", lambda: second)
    assert api.get("/chat/t1").json() == before
    ask(api, message="and then?")

    pseudonyms = {json.loads(line)["pseudonym"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()}
    assert len(pseudonyms) == 1


# one reply at a time per user: a second request while the first is streaming is refused
def test_second_request_while_streaming_gets_409(api, model):
    model.gate = threading.Event()
    model.replies += [AIMessage("first"), AIMessage("second")]
    first = threading.Thread(target=lambda: ask(api))
    first.start()
    deadline = time.time() + 5
    while not (chat._locks.get("u-1") and chat._locks["u-1"].locked()) and time.time() < deadline:
        time.sleep(0.01)

    busy = ask(api)

    assert busy.status_code == 409
    assert busy.json() == {"detail": "A reply is already in progress."}
    model.gate.set()
    first.join(timeout=10)
    assert ask(api).status_code == 200


def test_lock_released_after_error(api, model):
    model.replies += [RuntimeError("x"), AIMessage("fine")]
    ask(api)

    assert ask(api).status_code == 200


def wait_unlocked(user_id="u-1", seconds=3):
    deadline = time.time() + seconds
    while chat._locks[user_id].locked() and time.time() < deadline:
        time.sleep(0.01)
    return not chat._locks[user_id].locked()


# starlette closes the body iterator when the browser goes away; the lock must not leak
def test_lock_released_when_client_disconnects(api, model, users):
    model.replies += [AIMessage("one two three four"), AIMessage("later")]
    resp = chat.post_chat(chat.ChatRequest(thread_id="t1", message=QUESTION), users["u-1"])

    async def read_one_then_leave():
        first = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return first

    assert "event: sources" in asyncio.run(read_one_then_leave())
    assert wait_unlocked()
    assert ask(api).status_code == 200


# review on #9: if the browser leaves before starlette starts the body, the body never runs, so it can't
# be the thing that frees a lock the endpoint took; the lock is only taken once the body starts
def test_lock_not_held_when_body_never_starts(api, model, users):
    model.replies.append(AIMessage("later"))

    resp = chat.post_chat(chat.ChatRequest(thread_id="t1", message=QUESTION), users["u-1"])
    asyncio.run(resp.body_iterator.aclose())

    assert not chat._user_lock("u-1").locked()
    assert ask(api).status_code == 200


# review on #9: text streamed next to a tool call isn't the answer, so an empty final answer is still an error
def test_empty_answer_after_tool_call_text(api, model):
    call = tool_call("get_debts")
    call.content = "Let me check your debts"
    model.replies += [call, AIMessage(content="")]

    events = read_events(ask(api).text)

    assert events[-1] == ("error", {"message": "The model returned an empty answer. Try rephrasing."})


# leaving mid-answer must not stall the server: the model call finishes off the event loop
def test_disconnect_does_not_block_event_loop(api, model, users):
    model.token_delay = 0.3
    model.replies += [AIMessage("one two three four five six"), AIMessage("later")]
    resp = chat.post_chat(chat.ChatRequest(thread_id="t1", message=QUESTION), users["u-1"])

    async def leave_mid_answer():
        await resp.body_iterator.__anext__()          # sources
        await resp.body_iterator.__anext__()          # first token; the model is still writing
        gaps, last = [], time.monotonic()

        async def ticker():
            nonlocal last
            while True:
                await asyncio.sleep(0.02)
                gaps.append(time.monotonic() - last)
                last = time.monotonic()

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await resp.body_iterator.aclose()
        closed_in = time.monotonic() - started
        await asyncio.sleep(0.1)
        tick.cancel()
        return closed_in, max(gaps)

    closed_in, worst_gap = asyncio.run(leave_mid_answer())

    assert closed_in < 0.5 and worst_gap < 0.5, (closed_in, worst_gap)
    assert wait_unlocked()


# the next.js proxy drops a response after 30 s with no bytes; a cold 14b model can be silent that long
def test_keepalive_sent_while_model_silent(api, model, monkeypatch):
    monkeypatch.setattr(chat, "KEEPALIVE_SECONDS", 0.05)
    model.gate = threading.Event()
    threading.Timer(0.4, model.gate.set).start()
    model.replies.append(AIMessage(ANSWER))

    body = ask(api).text

    assert ": keep-alive\n\n" in body
    assert types(read_events(body))[-1] == "done"


def test_no_message_text_in_logs(api, model, caplog):
    model.replies += [tool_call("get_debts"), AIMessage(ANSWER)]

    with caplog.at_level(logging.DEBUG):
        ask(api)

    assert QUESTION not in caplog.text and ANSWER not in caplog.text


# the m2 done-when (spec amendment a1): the real model names the highest-apr card with its numbers,
# the app lists the cfpb guidance it retrieved, and the answer cites nothing the app didn't retrieve
@pytest.mark.skipif(not (ollama_has(config.OLLAMA_MODEL, config.OLLAMA_EMBED_MODEL) and KnowledgeBase().count() > 0),
                    reason="needs ollama with OLLAMA_MODEL and OLLAMA_EMBED_MODEL, and an ingested kb")
def test_live_chat_answers_which_debt_first(client, db, users, monkeypatch, tmp_path):
    monkeypatch.setattr(chat, "session_factory", db)
    monkeypatch.setattr(chat, "get_checkpointer", lambda: MemorySaver())
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")
    app.dependency_overrides[get_current_user] = lambda: users["u-1"]

    events = read_events(ask(client).text)
    text = "".join(d["text"] for e, d in events if e == "token")
    urls = [s["url"] for e, d in events if e == "sources" for s in d["sources"]]

    assert types(events)[-1] == "done", events[-1]
    assert "credit card" in text.lower()
    assert "22" in text
    assert any("how-reduce-your-debt" in u for u in urls), urls
    cited = [u.rstrip(".,") for u in re.findall(r"https?://[^\s)\]>\"']+", text)]
    assert all(u in urls for u in cited), cited
