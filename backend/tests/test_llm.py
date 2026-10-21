import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException, tool

from app import config
from app.agents.llm import call_with_tools, get_chat_model


# replays canned AIMessages in order and records what it was sent; stands in for ChatOllama
class ScriptedModel:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.seen.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# the toy tool: the model can't know the answer without calling it ("Ada" -> 24)
@tool
def get_lucky_number(name: str) -> int:
    """Return the lucky number for a person's name."""
    return len(name) * 7 + 3


def call(name, args, id="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


def test_get_chat_model_uses_config(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MODEL", "m1")
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://h:1")
    monkeypatch.setattr(config, "LLM_TEMPERATURE", 0.0)

    m = get_chat_model()

    assert (m.model, m.base_url, m.temperature) == ("m1", "http://h:1", 0.0)


def test_get_chat_model_overrides():
    m = get_chat_model(temperature=0.7, model="mistral")

    assert (m.model, m.temperature) == ("mistral", 0.7)


# the round trip: model asks for the tool, gets its result as a ToolMessage, answers with it
def test_tool_call_round_trips():
    model = ScriptedModel(call("get_lucky_number", {"name": "Ada"}), AIMessage("Ada's lucky number is 24."))

    answer = call_with_tools([HumanMessage("Ada's lucky number?")], [get_lucky_number], model=model)

    assert "24" in answer.content
    sent_back = model.seen[1][-1]
    assert isinstance(sent_back, ToolMessage)
    assert (sent_back.content, sent_back.tool_call_id) == ("24", "call-1")


def test_no_tool_call_returns_first_reply():
    model = ScriptedModel(AIMessage("hi"))

    answer = call_with_tools([HumanMessage("hello")], [get_lucky_number], model=model)

    assert answer.content == "hi"
    assert len(model.seen) == 1


# small models invent tool names; that should reach the model as an error, not crash the loop
def test_unknown_tool_gets_error_message():
    model = ScriptedModel(call("nope", {}), AIMessage("ok"))

    answer = call_with_tools([HumanMessage("x")], [get_lucky_number], model=model)

    assert answer.content == "ok"
    assert "unknown tool" in model.seen[1][-1].content


@tool
def broken(name: str) -> int:
    """Always fails."""
    raise ToolException("boom")


# a ToolException is the tool talking to the model ("bad category"), so its text is forwarded
def test_tool_exception_becomes_error_message():
    model = ScriptedModel(call("broken", {"name": "x"}), AIMessage("ok"))

    answer = call_with_tools([HumanMessage("x")], [broken], model=model)

    assert answer.content == "ok"
    assert "boom" in model.seen[1][-1].content


@tool
def leaky(name: str) -> int:
    """Fails like a database error that carries bound parameters."""
    raise RuntimeError("[SQL: select * from accounts where user_id=?] [parameters: ('u-jane',)]")


# any other exception's message can carry user data (sqlalchemy prints bound parameters),
# and this path bypasses the privacy gateway, so only the exception type reaches the model
def test_generic_exception_message_not_forwarded():
    model = ScriptedModel(call("leaky", {"name": "x"}), AIMessage("ok"))

    call_with_tools([HumanMessage("x")], [leaky], model=model)

    sent = model.seen[1][-1].content
    assert "RuntimeError" in sent
    assert "u-jane" not in sent and "SQL" not in sent


# ollama going away is not something to paper over with a fake answer
def test_model_error_propagates():
    model = ScriptedModel(ConnectionError("down"))

    with pytest.raises(ConnectionError):
        call_with_tools([HumanMessage("x")], [get_lucky_number], model=model)


def test_loop_stops_after_max_steps():
    model = ScriptedModel(*[call("get_lucky_number", {"name": "Ada"}, id=f"c{i}") for i in range(4)])

    with pytest.raises(RuntimeError, match="did not finish in 3 steps"):
        call_with_tools([HumanMessage("x")], [get_lucky_number], model=model, max_steps=3)


# skipped unless ollama answers /api/tags and lists the configured model
def _ollama_has_model() -> bool:
    try:
        resp = httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=2)
        return config.OLLAMA_MODEL in [m["name"] for m in resp.json()["models"]]
    except Exception:
        return False


# the ticket's done-when: a real tool call on the local model, end to end
@pytest.mark.skipif(not _ollama_has_model(), reason="needs ollama running with OLLAMA_MODEL pulled")
def test_live_tool_call_round_trip():
    calls = []

    @tool
    def get_lucky_number(name: str) -> int:
        """Return the lucky number for a person's name."""
        calls.append(name)
        return len(name) * 7 + 3

    answer = call_with_tools([HumanMessage("What is Ada's lucky number? Use the tool.")], [get_lucky_number])

    assert calls == ["Ada"]
    assert "24" in answer.content
