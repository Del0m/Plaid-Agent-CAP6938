from typing import Annotated, Callable, Optional
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from .llm import _run_tool, get_chat_model

# the m2 chat agent (architecture section 4): retrieve, then a hand-built react loop, agent <-> tools.
# hand-built rather than create_react_agent so tool errors go through _run_tool, which only
# forwards text meant for the model (the prebuilt ToolNode sends raw exception text back).
# the app retrieves guidance and lists the sources itself: in live runs neither llama3.1:8b nor
# qwen2.5:14b called search_kb next to get_debts, and both invented citations (spec amendment a1)

SYSTEM_PROMPT = (
    "You are a personal finance assistant for one user. For anything about the user's own accounts, "
    "debts or spending, call get_accounts, get_debts or get_spending and use only the numbers they return; "
    "never guess the user's numbers. For general rules and strategies, use the guidance below and refer to "
    "it by its title; never write URLs or name other websites, because the app lists the sources for you. "
    "If the guidance doesn't cover the question, call search_kb. When a figure applies to a specific year, "
    'say which year. Keep answers short and concrete. End with one line: "This is general information, '
    'not financial advice."'
)
# a model that keeps calling tools gets one last call without them, so the user still gets an answer
MAX_TOOL_ROUNDS = 6


def _merge(old: Optional[dict], new: Optional[dict]) -> dict:
    return {**(old or {}), **(new or {})}


class ChatState(MessagesState):
    pseudonym: str      # the privacy gateway's pseudonym for this thread, reused every turn
    tool_rounds: int    # tool rounds in the current turn; retrieve resets it to 0 per question
    sources: list[dict]  # guidance retrieved for the current question
    sources_by_turn: Annotated[dict[str, list[dict]], _merge]  # question message id -> its sources, for history

# what history shows under an answer; the passage text is only needed for the current question
CITATION_KEYS = ("title", "section", "source_url")
INTERRUPTED = "error: the previous turn ended before this tool returned"


def _guidance(sources: list[dict]) -> str:
    if not sources:
        return ""
    return "\n\nGuidance retrieved for this question:\n" + "\n".join(
        f"- {s['title']} / {s['section']}: {s['text']}" for s in sources)


# a turn cut off mid-tool (closed tab, ollama timeout) leaves tool calls with no results in the checkpoint.
# the model is sent a stand-in result for each, so every later turn's history stays well formed
def _answered(messages: list) -> list:
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        out.append(m)
        out += [ToolMessage(INTERRUPTED, tool_call_id=tc["id"], name=tc["name"])
                for tc in getattr(m, "tool_calls", None) or [] if tc["id"] not in answered]
    return out


def build_chat_graph(tools: list[BaseTool], *, retrieve: Optional[Callable[[str], list[dict]]] = None,
                     model: Optional[BaseChatModel] = None, checkpointer: Optional[BaseCheckpointSaver] = None):
    model = model or get_chat_model()
    by_name = {t.name: t for t in tools}

    def retrieve_guidance(state: ChatState) -> dict:
        question = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        found = retrieve(question.content) if retrieve else []
        # the round count is per question: resetting it here means no caller can forget to
        return {"sources": found, "tool_rounds": 0,
                "sources_by_turn": {question.id: [{k: s[k] for k in CITATION_KEYS} for s in found]}}

    # the system prompt and guidance are sent every call but never stored, so neither goes stale in old threads
    def agent(state: ChatState) -> dict:
        llm = model.bind_tools(tools) if state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS else model
        system = SystemMessage(SYSTEM_PROMPT + _guidance(state.get("sources", [])))
        return {"messages": [llm.invoke([system] + _answered(state["messages"]))]}

    def run_tools(state: ChatState) -> dict:
        calls = state["messages"][-1].tool_calls
        return {
            "messages": [ToolMessage(_run_tool(by_name, tc), tool_call_id=tc["id"], name=tc["name"]) for tc in calls],
            "tool_rounds": state.get("tool_rounds", 0) + 1,
        }

    def route(state: ChatState) -> str:
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    graph = StateGraph(ChatState)
    graph.add_node("retrieve", retrieve_guidance)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)
