import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import ToolException, tool
from langgraph.checkpoint.memory import MemorySaver

from app import config
from app.agents.chat_graph import MAX_TOOL_ROUNDS, SYSTEM_PROMPT, build_chat_graph
from tests.fakes import ScriptedChatModel, ollama_has, tool_call


# the model can't know the answer without calling it ("Ada" -> 24)
@tool
def get_lucky_number(name: str) -> int:
    """Return the lucky number for a person's name."""
    return len(name) * 7 + 3


@tool
def bad(name: str) -> int:
    """Rejects its argument the way a real tool does."""
    raise ToolException("bad arg")


@tool
def boom(name: str) -> int:
    """Fails like a database error that carries user data."""
    raise RuntimeError("secret user data 4321")


TOOLS = [get_lucky_number, bad, boom]
CONFIG = {"configurable": {"thread_id": "t1"}}


def run(model, question="q", tools=TOOLS, saver=None, **state):
    graph = build_chat_graph(tools, model=model, checkpointer=saver or MemorySaver())
    graph.invoke({"messages": [HumanMessage(question)], "tool_rounds": 0, **state}, CONFIG)
    return graph


def final(graph):
    return graph.get_state(CONFIG).values["messages"][-1]


def test_no_tool_call_returns_answer():
    model = ScriptedChatModel(replies=[AIMessage("hi there")])

    graph = run(model)

    assert final(graph).content == "hi there"
    assert isinstance(model.seen[0][0], SystemMessage) and model.seen[0][0].content == SYSTEM_PROMPT
    assert not any(isinstance(m, SystemMessage) for m in graph.get_state(CONFIG).values["messages"])


def test_tool_call_round_trips():
    model = ScriptedChatModel(replies=[tool_call("get_lucky_number", {"name": "Ada"}), AIMessage("It's 24.")])

    graph = run(model)

    sent = model.seen[1][-1]
    assert isinstance(sent, ToolMessage)
    assert (sent.tool_call_id, sent.content) == ("c1", "24")
    assert final(graph).content == "It's 24."


# small models invent tool names; that reaches the model as an error, not a crash
def test_unknown_tool_gets_error_message():
    model = ScriptedChatModel(replies=[tool_call("nope"), AIMessage("ok")])

    run(model)

    assert model.seen[1][-1].content.startswith("error: unknown tool")


# a ToolException is the tool talking to the model ("bad category"), so its text is forwarded
def test_tool_exception_text_reaches_model():
    model = ScriptedChatModel(replies=[tool_call("bad", {"name": "x"}), AIMessage("ok")])

    run(model)

    assert model.seen[1][-1].content == "error: bad arg"


# any other exception text can carry user data, so only its type reaches the model
def test_generic_exception_text_not_forwarded():
    model = ScriptedChatModel(replies=[tool_call("boom", {"name": "x"}), AIMessage("ok")])

    run(model)

    assert model.seen[1][-1].content == "error: boom failed (RuntimeError)"
    assert not any("4321" in str(m.content) for call in model.seen for m in call)


# ollama going away is not something to paper over with a fake answer
def test_model_error_propagates():
    model = ScriptedChatModel(replies=[ConnectionError("down")])

    with pytest.raises(ConnectionError):
        run(model)


# a model that never stops calling tools still gives the user an answer
def test_round_cap_then_answer_without_tools():
    calls = [tool_call("get_lucky_number", {"name": "Ada"}, id=f"c{i}") for i in range(MAX_TOOL_ROUNDS)]
    model = ScriptedChatModel(replies=calls + [AIMessage("best answer I have")])

    graph = run(model)

    assert MAX_TOOL_ROUNDS == 6
    assert len(model.seen) == 7
    assert all(t is not None for t in model.tools_seen[:6])
    assert model.tools_seen[6] is None
    assert final(graph).content == "best answer I have"


def test_tool_rounds_reset_each_turn():
    saver = MemorySaver()
    model = ScriptedChatModel(replies=[tool_call("get_lucky_number", {"name": "A"}, id="a"),
                                       tool_call("get_lucky_number", {"name": "B"}, id="b"),
                                       AIMessage("turn one"), AIMessage("turn two")])
    graph = run(model, saver=saver)

    graph.invoke({"messages": [HumanMessage("again")], "tool_rounds": 0}, CONFIG)

    assert model.tools_seen[-1] is not None


def test_memory_across_turns():
    saver = MemorySaver()
    model = ScriptedChatModel(replies=[AIMessage("first answer"), AIMessage("second answer")])
    graph = run(model, question="first question", saver=saver)

    graph.invoke({"messages": [HumanMessage("second question")], "tool_rounds": 0}, CONFIG)

    contents = [m.content for m in model.seen[-1]]
    assert "first question" in contents and "first answer" in contents


def test_pseudonym_kept_in_state():
    saver = MemorySaver()
    model = ScriptedChatModel(replies=[AIMessage("a"), AIMessage("b")])
    graph = run(model, saver=saver, pseudonym="user_ab12")

    graph.invoke({"messages": [HumanMessage("again")], "tool_rounds": 0}, CONFIG)

    assert graph.get_state(CONFIG).values["pseudonym"] == "user_ab12"


# a real tool call on the local model, end to end through the graph
@pytest.mark.skipif(not ollama_has(config.OLLAMA_MODEL), reason="needs ollama running with OLLAMA_MODEL pulled")
def test_live_tool_call_round_trip():
    graph = build_chat_graph([get_lucky_number], checkpointer=MemorySaver())

    graph.invoke({"messages": [HumanMessage("What is Ada's lucky number? Use the tool.")], "tool_rounds": 0},
                 CONFIG)

    assert "24" in final(graph).content
